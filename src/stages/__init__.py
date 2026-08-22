"""Pipeline stages. Each wraps a sibling repo's own CLI via the tee runner."""


def b(value: bool) -> str:
    """Render a bool as the 'true'/'false' string the sub-repo CLIs expect."""
    return "true" if value else "false"
