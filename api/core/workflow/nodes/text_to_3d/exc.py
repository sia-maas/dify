class TextTo3DNodeError(ValueError):
    """Base class for TextTo3DNode errors."""


class InvalidModelTypeError(TextTo3DNodeError):
    """Raised when the model is not a Large Language Model."""
