"""CSV cells shared by panel downloads."""


def clean_cell(value) -> str:
    value = "" if value is None else str(value)
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value
