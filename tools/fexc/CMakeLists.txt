# Native macOS arm64 build of the FEXCore compiler subset plus the fexc driver.
# Not FEX's own top-level CMake (it assumes Linux): this lists the sources
# FEXCore's CMake would build and omits the Linux application layer.
# Usage: cmake -S tools/fexc -B BUILD -DFEXC_SRC=<patched FEX tree> (see build.sh)
cmake_minimum_required(VERSION 3.20)
project(fexc LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS ON)
if(NOT FEXC_SRC)
  message(FATAL_ERROR "-DFEXC_SRC=<patched FEX tree> required")
endif()
set(S ${FEXC_SRC})
set(GEN ${CMAKE_BINARY_DIR}/gen)
set(FEXC_SHIM ${CMAKE_CURRENT_SOURCE_DIR}/shim)

# ---- generated files (same generators FEXCore's CMake runs) ----
file(MAKE_DIRECTORY ${GEN}/include/FEXCore/IR ${GEN}/include/FEXCore/Config ${GEN}/generated/Config)
set(CMAKE_INSTALL_FULL_LIBDIR /unused)
set(CMAKE_INSTALL_PREFIX /unused)
configure_file(${S}/FEXCore/Source/Interface/Config/Config.json.in ${GEN}/generated/Config/Config.json)
file(WRITE ${GEN}/include/git_version.h
"#pragma once
#include <array>
#include <cstdint>
static constexpr std::array<uint8_t, 20> GIT_HASH = {0};
#define GIT_DESCRIBE_STRING \"natemu-fexc-48d71752\"
")
add_custom_command(
  OUTPUT ${GEN}/include/FEXCore/IR/IRDefines.inc ${GEN}/include/FEXCore/IR/IRDefines_Dispatch.inc
  DEPENDS ${S}/FEXCore/Source/Interface/IR/IR.json ${S}/FEXCore/Scripts/json_ir_generator.py
  COMMAND python3 ${S}/FEXCore/Scripts/json_ir_generator.py ${S}/FEXCore/Source/Interface/IR/IR.json
          ${GEN}/include/FEXCore/IR/IRDefines.inc ${GEN}/include/FEXCore/IR/IRDefines_Dispatch.inc)
add_custom_command(
  OUTPUT ${GEN}/include/FEXCore/Config/ConfigValues.inl ${GEN}/include/FEXCore/Config/ConfigOptions.inl ${GEN}/FEX.1
  DEPENDS ${GEN}/generated/Config/Config.json ${S}/FEXCore/Scripts/config_generator.py
  COMMAND python3 ${S}/FEXCore/Scripts/config_generator.py ${GEN}/generated/Config/Config.json
          ${GEN}/include/FEXCore/Config/ConfigValues.inl ${GEN}/FEX.1 ${GEN}/include/FEXCore/Config/ConfigOptions.inl)
add_custom_target(fexc_gen DEPENDS
  ${GEN}/include/FEXCore/IR/IRDefines.inc ${GEN}/include/FEXCore/Config/ConfigValues.inl)

# ---- third-party (all vendored in the pinned tree) ----
add_library(fmt STATIC ${S}/External/fmt/src/format.cc)
target_include_directories(fmt PUBLIC ${S}/External/fmt/include)
add_library(xxhash STATIC ${S}/External/xxhash/xxhash.c)
target_include_directories(xxhash PUBLIC ${S}/External/xxhash)
add_subdirectory(${S}/External/SoftFloat-3e softfloat_3e_dir)
add_subdirectory(${S}/External/cephes cephes_dir)

# ---- FEXCore (source list = FEXCore/Source/CMakeLists.txt minus the Linux-only
# pieces; see README "excluded/shimmed") ----
set(FC ${S}/FEXCore/Source)
set(FEXCORE_SRCS
  Interface/Config/Config.cpp
  Utils/Allocator.cpp Utils/AllocatorHooks.cpp Utils/FileLoading.cpp Utils/ForcedAssert.cpp Utils/LogManager.cpp
  Utils/SpinWaitLock.cpp Utils/WildcardMatcher.cpp
  Common/JitSymbols.cpp
  Interface/Context/Context.cpp
  Interface/Core/LookupCache.cpp Interface/Core/DiskCache.cpp Interface/Core/CodeCache.cpp
  Interface/Core/Core.cpp Interface/Core/CPUBackend.cpp Interface/Core/Addressing.cpp
  Interface/Core/CPUID.cpp Interface/Core/Frontend.cpp Interface/Core/SharedCodeBufferManager.cpp
  Interface/Core/OpcodeDispatcher/AVX_128.cpp Interface/Core/OpcodeDispatcher/Crypto.cpp
  Interface/Core/OpcodeDispatcher/Flags.cpp Interface/Core/OpcodeDispatcher/Vector.cpp
  Interface/Core/OpcodeDispatcher/X87.cpp Interface/Core/OpcodeDispatcher/X87F64.cpp
  Interface/Core/OpcodeDispatcher.cpp
  Interface/Core/ArchHelpers/Arm64Emitter.cpp
  Interface/Core/Dispatcher/Dispatcher.cpp
  Interface/Core/Interpreter/Fallbacks/InterpreterFallbacks.cpp
  Interface/Core/Interpreter/Fallbacks/StringCompareFallbacks.cpp
  Interface/Core/JIT/JIT.cpp Interface/Core/JIT/ALUOps.cpp Interface/Core/JIT/AtomicOps.cpp
  Interface/Core/JIT/BranchOps.cpp Interface/Core/JIT/ConversionOps.cpp
  Interface/Core/JIT/EncryptionOps.cpp Interface/Core/JIT/MemoryOps.cpp
  Interface/Core/JIT/MiscOps.cpp Interface/Core/JIT/MoveOps.cpp Interface/Core/JIT/VectorOps.cpp
  Interface/Core/JIT/Arm64Relocations.cpp
  Interface/Core/X86Tables/BaseTables.cpp Interface/Core/X86Tables/DDDTables.cpp
  Interface/Core/X86Tables/H0F38Tables.cpp Interface/Core/X86Tables/H0F3ATables.cpp
  Interface/Core/X86Tables/PrimaryGroupTables.cpp Interface/Core/X86Tables/SecondaryGroupTables.cpp
  Interface/Core/X86Tables/SecondaryModRMTables.cpp Interface/Core/X86Tables/SecondaryTables.cpp
  Interface/Core/X86Tables/VEXTables.cpp Interface/Core/X86Tables/X87Tables.cpp
  Interface/GDBJIT/GDBJIT.cpp
  Interface/IR/IRDumper.cpp Interface/IR/IREmitter.cpp Interface/IR/PassManager.cpp
  Interface/IR/Passes/IRDumperPass.cpp Interface/IR/Passes/IRValidation.cpp
  Interface/IR/Passes/RedundantFlagCalculationElimination.cpp
  Interface/IR/Passes/RegisterAllocationPass.cpp Interface/IR/Passes/x87StackOptimizationPass.cpp
  Utils/LongJump.cpp Utils/Telemetry.cpp Utils/Threads.cpp Utils/WorkQueueThread.cpp Utils/Profiler.cpp
  Utils/Allocator/64BitAllocator.cpp
  Utils/ArchHelpers/Arm64.cpp)
list(TRANSFORM FEXCORE_SRCS PREPEND ${FC}/)
# Replacement/stub translation units for Linux-only pieces live in shim/.
file(GLOB FEXC_STUBS ${FEXC_SHIM}/*.cpp)

add_library(fexcore STATIC ${FEXCORE_SRCS} ${FEXC_STUBS})
add_dependencies(fexcore fexc_gen)
target_compile_definitions(fexcore PUBLIC
  JIT_ARM64 ARCHITECTURE_arm64=1 FEX_DISABLE_TELEMETRY=1 _LARGEFILE64_SOURCE
  FEXCORE_PRESERVE_ALL_ATTR= FEXCORE_HAS_PRESERVE_ALL_ATTR=0
  FEX_PRESERVE_ALL_ATTR= FEX_HAS_PRESERVE_ALL_ATTR=0 HAS_PROGRAM_INVOCATION_NAME=0)
target_include_directories(fexcore PUBLIC
  ${FEXC_SHIM} ${GEN}/include ${GEN}/generated
  ${S}/FEXCore/include ${FC} ${S}/FEXHeaderUtils ${S}/CodeEmitter
  ${S}/External/unordered_dense/include ${S}/External/cephes/include
  ${S}/External/SoftFloat-3e/source/include)
target_compile_options(fexcore PUBLIC
  -include ${FEXC_SHIM}/fexc_compat.h
  $<$<COMPILE_LANGUAGE:CXX>:-fno-strict-aliasing> $<$<COMPILE_LANGUAGE:CXX>:-fno-exceptions>
  -fwrapv -Wno-trigraphs -w)
target_link_libraries(fexcore PUBLIC fmt xxhash cephes_128bit)

add_executable(fexc_linktest ${CMAKE_CURRENT_SOURCE_DIR}/linktest.cpp)
target_link_libraries(fexc_linktest PRIVATE fexcore)

add_executable(fexc ${CMAKE_CURRENT_SOURCE_DIR}/fexc.cpp)
target_link_libraries(fexc PRIVATE fexcore)
