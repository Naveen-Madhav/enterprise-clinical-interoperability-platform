class InterfaceError(Exception):
    """A bounded diagnostic safe to record without the source payload."""

    def __init__(self, code: str, *, retryable: bool = False, ack: str = "AE"):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.ack = ack
