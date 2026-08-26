"""Photo ya PDF me se text nikalna.

Flask aur DB se koi lena-dena nahi — sirf bytes andar, text bahar. Isi wajah se
ise alag se test kiya ja sakta hai, aur kal engine badalna ho (Tesseract ki
jagah koi cloud OCR) toh sirf yahi file badlegi.

Teen raaste, sabse bharose wale se shuru:

1. PDF jisme text pehle se hai — `pdftotext`. Yahan OCR ki zaroorat hi nahi,
   aur nateeja bilkul sahi aata hai.
2. PDF jo asal me scan hai — pehle page ki tasveer banao, phir OCR.
3. Photo — seedha OCR.

OCR khud kabhi poora sahi nahi padhta; `GME01` ko `GMEO1` bana dena aam baat
hai. Us galti ko sudhaarne ka kaam yahan nahi hota — wo po_module me hota hai,
jahan us party ke asli code maujood hain.
"""
import os
import shutil
import subprocess
import tempfile

# Scan ka pehla page hi kaafi hai. Order ki lines pehle page pe hi hoti hain,
# aur baaki page OCR karke waqt aur galtiyan dono badhti hain.
PDF_PAGES = 2
OCR_DPI = 300
TIMEOUT = 60


class OcrUnavailable(Exception):
    """Is machine pe OCR ka saamaan nahi hai."""


class OcrFailed(Exception):
    """Saamaan hai par is file se text nahi nikla."""


def have_tool(name):
    return shutil.which(name) is not None


def available():
    """OCR ho sakta hai ya nahi — screen ko yahi batana hota hai."""
    return have_tool("tesseract")


def _run(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=TIMEOUT, **kw)
    except FileNotFoundError as exc:
        raise OcrUnavailable(str(exc))
    except subprocess.TimeoutExpired:
        raise OcrFailed("file padhne me bahut waqt lag gaya")


def _tesseract(path):
    if not have_tool("tesseract"):
        raise OcrUnavailable("tesseract nahi mila")
    # stdout pe text — beech me koi file nahi banti
    res = _run(["tesseract", path, "stdout", "--psm", "6"])
    if res.returncode != 0:
        raise OcrFailed((res.stderr or b"").decode("utf8", "ignore")[:200])
    return res.stdout.decode("utf8", "ignore")


def _pdf_text_layer(path):
    """PDF me text pehle se ho toh wahi lo — ye OCR se hamesha behtar hai."""
    if not have_tool("pdftotext"):
        return ""
    res = _run(["pdftotext", "-l", str(PDF_PAGES), "-layout", path, "-"])
    if res.returncode != 0:
        return ""
    return res.stdout.decode("utf8", "ignore")


def _pdf_via_images(path, workdir):
    if not have_tool("pdftoppm"):
        raise OcrUnavailable("pdftoppm nahi mila")
    stem = os.path.join(workdir, "page")
    res = _run(["pdftoppm", "-png", "-r", str(OCR_DPI),
                "-l", str(PDF_PAGES), path, stem])
    if res.returncode != 0:
        raise OcrFailed("PDF ki tasveer nahi ban payi")
    pages = sorted(f for f in os.listdir(workdir) if f.startswith("page"))
    if not pages:
        raise OcrFailed("PDF me koi page nahi mila")
    return "\n".join(_tesseract(os.path.join(workdir, p)) for p in pages)


def read_text(data, mime="", filename=""):
    """Bytes lo, text do. Kuch na mile toh OcrFailed.

    `mime` par poora bharosa nahi karte — browser kabhi kuch bhi bhej deta hai.
    Isliye naam aur file ke pehle chaar byte, dono dekhe jaate hain.
    """
    if not data:
        raise OcrFailed("file khaali hai")

    is_pdf = (data[:4] == b"%PDF"
              or "pdf" in (mime or "").lower()
              or (filename or "").lower().endswith(".pdf"))

    with tempfile.TemporaryDirectory() as workdir:
        path = os.path.join(workdir, "in.pdf" if is_pdf else "in.img")
        with open(path, "wb") as fh:
            fh.write(data)

        if is_pdf:
            text = _pdf_text_layer(path)
            # Do-chaar akshar ka matlab hai ki PDF asal me scan hai
            if len(text.strip()) < 20:
                text = _pdf_via_images(path, workdir)
        else:
            text = _tesseract(path)

    if not text.strip():
        raise OcrFailed("is file me koi text nahi mila")
    return text
