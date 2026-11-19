"""Read local frontend files within the configured build directory."""

from dataclasses import dataclass
from mimetypes import guess_type
from pathlib import Path

_BUILD_HINT = (
    b"The frontend build is missing. Run `cd frontend && pnpm install && pnpm build` "
    b"from the workbench repository. See frontend/README.md.\n"
)


@dataclass(frozen=True, slots=True)
class FrontendResponse:
    status: int
    payload: bytes
    content_type: str
    cache_control: str = "no-store"


def _contained_path(root: Path, path: Path) -> Path:
    try:
        resolved = path.resolve()
    except RuntimeError:  # A symlink loop on Python 3.12.
        raise KeyError("frontend file") from None
    if not resolved.is_relative_to(root):
        raise KeyError("frontend file")
    return resolved


def read_frontend(root: Path, parts: list[str]) -> FrontendResponse:
    if any("/" in part or "\0" in part for part in parts):
        raise KeyError("frontend file")
    if not root.is_dir():
        if parts == [""]:
            return FrontendResponse(404, _BUILD_HINT, "text/plain; charset=utf-8")
        raise KeyError("frontend file")

    requested = root.joinpath(*parts)
    index = root / "index.html"
    file = _contained_path(root, requested)
    if not file.is_file():
        if parts[0] == "assets" and len(parts) > 1:
            raise KeyError("frontend file")
        requested = index
        file = _contained_path(root, index)
        if not file.is_file():
            raise KeyError("frontend file")
    if requested == index:
        return FrontendResponse(
            200, file.read_bytes(), "text/html; charset=utf-8", "no-cache"
        )
    content_type = guess_type(file.name)[0] or "application/octet-stream"
    return FrontendResponse(200, file.read_bytes(), content_type)
