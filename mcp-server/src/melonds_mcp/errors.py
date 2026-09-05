"""Domain errors that can be returned to an MCP caller without a traceback."""


class MelonDSMCPError(RuntimeError):
    """Base class for expected melonDS MCP failures."""


class ValidationError(MelonDSMCPError):
    """A tool argument is invalid or exceeds a safety limit."""


class SessionError(MelonDSMCPError):
    """The requested emulator/core session is not available."""


class BridgeProtocolError(MelonDSMCPError):
    """The native melonDS bridge sent or received an invalid frame."""


class BridgeConnectionError(MelonDSMCPError):
    """The local native bridge stream could not be reached or was lost."""


class BridgeRemoteError(MelonDSMCPError):
    """The native bridge returned a structured operation failure."""

    def __init__(self, code: str, message: str, details: object = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details


class RspError(MelonDSMCPError):
    """Base class for GDB Remote Serial Protocol failures."""


class RspConnectionError(RspError):
    """The TCP connection to a melonDS GDB stub failed."""


class RspProtocolError(RspError):
    """The peer sent an invalid or unexpected RSP message."""


class RspRemoteError(RspError):
    """The melonDS GDB stub returned an E-prefixed error packet."""
