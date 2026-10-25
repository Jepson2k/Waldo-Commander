"""Open the extracted review ZIP offline and decode its screenshots and videos."""

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import shutil
import argparse
from zipfile import ZipFile

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait


def verify(root: Path | None = None) -> dict:
    root = (
        root or Path(__file__).resolve().parents[1] / ".scene-evidence" / "review"
    ).resolve()
    pages = json.loads((root / "galleries.json").read_text())
    options = webdriver.ChromeOptions()
    options.binary_location = shutil.which("chromium") or ""
    for flag in (
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1920,1080",
    ):
        options.add_argument(flag)
    driver = webdriver.Chrome(
        service=Service(shutil.which("chromedriver")), options=options
    )
    driver.set_page_load_timeout(30)
    driver.set_script_timeout(30)
    try:
        with TemporaryDirectory(prefix="scene-gallery-") as isolated:
            with ZipFile(root / "scene-review.zip") as bundle:
                assert bundle.testzip() is None
                bundle.extractall(isolated)
            results = []
            for spec in pages:
                page = Path(isolated) / spec["file"]
                assert page.stat().st_size <= 3_000_000, spec
                driver.get(page.as_uri())
                links = driver.execute_script(
                    "return [...document.querySelectorAll('a[href]')].map(a=>a.getAttribute('href'))"
                )
                for link in links:
                    assert (Path(isolated) / link).is_file(), (spec["file"], link)
                driver.execute_script(
                    "for (const image of document.images) image.loading='eager'"
                )
                WebDriverWait(driver, 30).until(
                    lambda d: d.execute_script(
                        "return [...document.images].every(i=>i.complete)"
                    )
                )
                result = driver.execute_script("""return {
                    images:document.images.length,
                    imagesDecoded:[...document.images].filter(i=>i.naturalWidth>0).length,
                    brokenImages:[...document.images].filter(i=>!i.naturalWidth).map(i=>i.alt),
                    videos:document.querySelectorAll('video').length,
                    externalMedia:[...document.querySelectorAll('img,video')].filter(e=>!e.src.startsWith('data:') && !e.src.startsWith('file:')).length
                }""")
                result.update(
                    file=spec["file"],
                    bytes=page.stat().st_size,
                    videosPlayed=0,
                    videoErrors=[],
                )
                for index, video in enumerate(
                    driver.find_elements("css selector", "video")
                ):
                    status = driver.execute_async_script(
                        """
                        const video=arguments[0], done=arguments[1]; video.muted=true;
                        video.play().then(()=>done({ok:true})).catch(e=>done({ok:false,error:String(e)}));
                        """,
                        video,
                    )
                    if not status["ok"]:
                        result["videoErrors"].append({"index": index, **status})
                        continue
                    WebDriverWait(driver, 20).until(
                        lambda d: d.execute_script(
                            "return arguments[0].currentTime>0.05 && arguments[0].getVideoPlaybackQuality().totalVideoFrames>0",
                            video,
                        )
                    )
                    driver.execute_script("arguments[0].pause()", video)
                    result["videosPlayed"] += 1
                if spec["kind"] == "feature" or spec["file"] == "features.html":
                    driver.save_screenshot(str(root / f"{page.stem}-media-check.png"))
                assert result["imagesDecoded"] == result["images"], result
                assert result["videosPlayed"] == result["videos"], result
                assert result["externalMedia"] == 0, result
                results.append(result)
            result = {
                "pages": results,
                "imagesDecoded": sum(r["imagesDecoded"] for r in results),
                "videosPlayed": sum(r["videosPlayed"] for r in results),
                "largestPageBytes": max(r["bytes"] for r in results),
                "method": "ZIP extracted to an isolated directory and opened offline in Chromium using file URLs. Every navigation target exists, each image decodes, and every video plays without a server or the original workspace directories.",
            }
            (root / "media-verification.json").write_text(
                json.dumps(result, indent=2) + "\n"
            )
            return result
    finally:
        driver.quit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?")
    print(json.dumps(verify(parser.parse_args().directory), indent=2))
