// C1a link smoke test: references the FEXCore Context/Config API so the whole
// static library (frontend, IR, passes, RA, ARM64 emitter) is pulled in at link time.
// With --init it also runs InitCore() (allocates the dispatcher/JIT buffers); no guest code is compiled.
#include <FEXCore/Config/Config.h>
#include <FEXCore/Core/Context.h>
#include <FEXCore/Core/HostFeatures.h>
#include <FEXCore/Core/SignalDelegator.h>
#include <FEXCore/HLE/SyscallHandler.h>
#include <FEXCore/fextl/memory.h>
#include <cstdio>
#include <cstring>

namespace {
struct NoSyscalls final : FEXCore::HLE::SyscallHandler {
  void HandleSyscall(FEXCore::Core::CpuStateFrame*) override {}
  FEXCore::HLE::ExecutableRangeInfo QueryGuestExecutableRange(FEXCore::Core::InternalThreadState*, uint64_t) override { return {0, UINT64_MAX, true}; }
  std::optional<FEXCore::ExecutableFileSectionInfo> LookupExecutableFileSection(FEXCore::Core::InternalThreadState*, uint64_t) override { return std::nullopt; }
};
struct NoSignals final : FEXCore::SignalDelegator {};
}

int main(int argc, char** argv) {
  FEXCore::Config::Initialize();
  FEXCore::Config::Set(FEXCore::Config::CONFIG_IS64BIT_MODE, "1");
  FEXCore::HostFeatures features {};
  features.SupportsAtomics = true;
  auto ctx = FEXCore::Context::Context::CreateNewContext(features);
  std::printf("context created: %p\n", static_cast<void*>(ctx.get()));
  if (argc > 1 && !std::strcmp(argv[1], "--init")) {
    NoSyscalls sys; NoSignals sig;
    ctx->SetSignalDelegator(&sig);
    ctx->SetSyscallHandler(&sys);
    std::printf("InitCore: %s\n", ctx->InitCore() ? "ok" : "failed");
  }
  return 0;
}
