"""Build the local scene review gallery from explicitly listed evidence sets."""

from __future__ import annotations

import html
import json
import base64
import mimetypes
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / ".scene-evidence" / "review"


def render(progress: dict, *, embedded: bool, recordings: dict[Path, str]) -> str:
    page_title = html.escape(progress.get("title", "Native scene · review gallery"))
    intro = html.escape(
        progress.get(
            "intro",
            "Running-app evidence, review status and reproduction details. Completed means the feature has passed its scoped checks and review rounds. Partial work is listed explicitly.",
        )
    )

    def media(path: Path) -> str:
        if not path.is_file():
            raise FileNotFoundError(f"Review media missing: {path}")
        if embedded:
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
        return str(path.relative_to(ROOT / ".scene-evidence"))

    def document(path: Path, title: str) -> str:
        if embedded:
            return (
                f"<details><summary>{html.escape(title)}</summary>"
                f"<pre>{html.escape(path.read_text())}</pre></details>"
            )
        return f'<a href="{media(path)}">{html.escape(title)}</a>'

    sections = []
    for feature in progress["features"]:
        title = html.escape(feature["title"])
        heading = f"<h2>{title}</h2>" if len(progress["features"]) > 1 else ""
        body = [
            f'<section id="{feature["id"]}"><header>{heading}<span>{html.escape(feature["status"])}</span></header>'
        ]
        if len(progress["features"]) > 1:
            body.append(f"<p>{html.escape(feature['summary'])}</p>")
        for evidence in feature.get("evidence", []):
            directory = ROOT / evidence["directory"]
            manifest_path = directory / "manifest.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                body.append(
                    f'<p class="meta">{html.escape(evidence["label"])} · commit {html.escape(manifest["commit"][:12])}</p>'
                    + document(manifest_path, "Capture manifest & reproduction")
                )
            for capture in evidence.get("screenshots", []):
                body.append(
                    f'<img loading="lazy" src="{media(directory / capture)}" alt="{title}: {html.escape(capture)}">'
                )
            if directory in recordings:
                body.append(
                    f'<p><a href="{recordings[directory]}">Watch interaction · {html.escape(evidence["label"])}</a></p>'
                )
            if not embedded and (directory / "interaction.gif").exists():
                body.append(
                    f'<p><a href="{media(directory / "interaction.gif")}">Original animated GIF</a></p>'
                )
            if (directory / "test-results.xml").exists():
                body.append(document(directory / "test-results.xml", "Test results"))
            reports = sorted(
                directory.glob("*review*.md"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if reports:
                body.append(
                    "<details><summary>Review reports</summary>"
                    + "\n".join(document(report, report.stem) for report in reports)
                    + "</details>"
                )
        for note in feature.get("notes", []):
            body.append(f'<p class="meta">{html.escape(note)}</p>')
        body.append("</section>")
        sections.append("\n".join(body))
    return page(
        page_title,
        f'<p>{intro}</p><p class="meta">{html.escape(progress["updated"])} · Full-resolution screenshots. Recordings open separately; original GIFs are included in the downloadable bundle.</p>'
        + "\n".join(sections),
    )


def page(title: str, body: str, *, back: str = "features.html") -> str:
    return (
        """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PAGE_TITLE</title><style>
:root{color-scheme:dark}body{margin:0;background:#101317;color:#e9edf3;font:16px/1.6 system-ui,sans-serif}main{max-width:1200px;margin:auto;padding:40px 24px}h1{font-size:36px;line-height:1.2;margin:0 0 12px}h2{font-size:24px;margin:0}p{max-width:85ch}a{color:#79c8ff}section{margin:32px 0;border-top:1px solid #37404c;padding-top:24px}header{display:flex;justify-content:space-between;align-items:center;gap:16px}header span{border:1px solid #526274;padding:3px 12px;border-radius:20px;font-size:13px;white-space:nowrap}img,video{display:block;max-width:100%;height:auto;margin:16px 0;border-radius:8px;border:1px solid #37404c}video{width:100%}.meta{font-size:14px;color:#a9b5c3}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:32rem;overflow:auto;font:13px/1.5 ui-monospace,monospace}nav{display:flex;gap:16px;flex-wrap:wrap;margin:24px 0}@media(max-width:600px){main{padding:24px 12px}header{align-items:start;flex-direction:column}}
</style><main>""".replace("PAGE_TITLE", title)
        + (f'<nav><a href="{back}">← Back to galleries</a></nav>' if back else "")
        + f"<h1>{title}</h1>"
        + body
        + "</main></html>"
    )


def build() -> Path:
    progress = json.loads((DESTINATION / "progress.json").read_text())
    pages = []
    portable = {}
    index = [
        "<p>Choose one feature to review. Each gallery contains screenshots; its short recordings open separately.</p>"
    ]

    def save(name: str, title: str, content: str, kind: str) -> None:
        destination = DESTINATION / name
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(content)
        size = temporary.stat().st_size
        if size > 3_000_000:
            temporary.unlink()
            raise ValueError(
                f"Review page exceeds the 3 MB preview budget: {name} ({size} bytes)"
            )
        temporary.replace(destination)
        pages.append({"file": name, "title": title, "kind": kind, "bytes": size})

    for feature in progress["features"]:
        name = f"{feature['id']}.html"
        title = html.escape(feature["title"])
        recordings = {}
        for i, evidence in enumerate(feature.get("evidence", []), 1):
            directory = ROOT / evidence["directory"]
            video = directory / "interaction.mp4"
            if not video.exists():
                continue
            recording = f"{feature['id']}-recording-{i}.html"
            recordings[directory] = recording
            heading = f"{title} · {html.escape(evidence['label'])}"
            source = base64.b64encode(video.read_bytes()).decode("ascii")
            content = page(
                heading,
                f'<video controls playsinline preload="metadata" src="data:video/mp4;base64,{source}" aria-label="{heading}"></video>',
                back=name,
            )
            save(
                recording,
                f"{feature['title']} · {evidence['label']}",
                content,
                "recording",
            )
            portable[recording] = content
        subset = {
            **progress,
            "title": feature["title"],
            "intro": feature["summary"],
            "features": [feature],
        }
        save(
            name,
            feature["title"],
            render(subset, embedded=True, recordings=recordings),
            "feature",
        )
        portable[name] = render(subset, embedded=False, recordings=recordings)
        size = pages[-1]["bytes"] / 1_000_000
        index.append(
            f'<section><h2><a href="{name}">{title}</a></h2><p>{html.escape(feature["summary"])}</p><p class="meta">{size:.2f} MB · {html.escape(feature["status"])}</p></section>'
        )
    content = page("Scene review · feature galleries", "\n".join(index), back="")
    for name in ("features.html", "index.html", "corrections.html"):
        save(name, "Scene review · feature galleries", content, "index")
        portable[name] = content
    (DESTINATION / "galleries.json").write_text(json.dumps(pages, indent=2) + "\n")
    with ZipFile(DESTINATION / "scene-review.zip", "w", ZIP_DEFLATED) as bundle:
        root = ROOT / ".scene-evidence"
        for name, content in portable.items():
            bundle.writestr(name, content)
        bundle.write(DESTINATION / "progress.json", "progress.json")
        included = set()
        for feature in progress["features"]:
            for evidence in feature.get("evidence", []):
                directory = ROOT / evidence["directory"]
                for path in directory.iterdir():
                    if (
                        path not in included
                        and path.is_file()
                        and path.suffix
                        in {
                            ".png",
                            ".gif",
                            ".mp4",
                            ".json",
                            ".xml",
                            ".md",
                        }
                    ):
                        bundle.write(path, path.relative_to(root))
                        included.add(path)
    return DESTINATION / "features.html"


if __name__ == "__main__":
    print(build())
