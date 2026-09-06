import Dispatch

// Only public DispatchTime operations interpret the opaque C token.
@_cdecl("t2_dispatch_deadline")
public func t2DispatchDeadline(_ requested: UInt64,
                               _ represented: UnsafeMutablePointer<UInt64>) -> UInt64 {
    let deadline = DispatchTime(uptimeNanoseconds: requested)
    represented.pointee = deadline.uptimeNanoseconds
    return deadline.rawValue
}
