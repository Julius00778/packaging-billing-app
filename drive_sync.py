"""Google Drive se product samples uthana.

Photos Drive me rehti hain — ek main folder, uske andar har party ka apna folder,
aur har file ka naam item code + size hota hai:

    SAMBHAV/
      GM ENTERPREISES/
        GME01(31x15x5).jpeg
        GME02(23x14x4).jpeg
      SAMEER METALS/
        SM01(32x24x3.5).jpeg

Har baar Drive se photo maangna slow hai aur file ka naam badalte hi tut jaata
hai. Isliye sync ek baar chalta hai: folder padho, naam se code aur size nikaalo,
photo download karke chhoti karke DB me rakh do. Uske baad Drive band ho tab bhi
order flow chalta rahega.

App ko Drive me service account se entry milti hai — ek program ka email address.
Usse sirf wahi folder dikhta hai jo uske saath share kiya gaya hai.
"""

import io
import json
import os
import re

MIME_FOLDER = "application/vnd.google-apps.folder"
IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp", "image/heic", "image/heif")

# "GME01(31x15x5).jpeg" — shuru me code, phir brackets me size.
# Space, hyphen, chhote akshar — sab chalta hai: "GME 01 (31x15x5)" bhi wahi hai.
CODE_AT_START_RE = re.compile(r"^\s*(?P<code>[A-Za-z]{1,6}\s*-?\s*\d{1,4})(?![A-Za-z0-9])")


class DriveError(Exception):
    """Setup ki galti — message seedha operator ko dikhaya jaata hai."""


def credentials_from_env(env_var="GOOGLE_SERVICE_ACCOUNT_JSON"):
    """Service account ki key environment se lo. Key kabhi code me nahi aati."""
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        raise DriveError(
            f"{env_var} set nahi hai. Railway ke is environment me service account "
            "ki JSON key daalni padegi."
        )
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        raise DriveError(
            f"{env_var} me poora JSON nahi hai — file ka saara content copy karke "
            "dobara paste karo (shuru ka {{ aur aakhir ka }} bhi)."
        )
    try:
        from google.oauth2 import service_account
    except ImportError:
        raise DriveError("google-auth install nahi hai — requirements.txt dekho.")
    return service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )


def drive_service(creds=None):
    try:
        from googleapiclient.discovery import build
    except ImportError:
        raise DriveError("google-api-python-client install nahi hai — requirements.txt dekho.")
    return build("drive", "v3", credentials=creds or credentials_from_env(),
                 cache_discovery=False)


def folder_id_from_link(text):
    """Drive ka poora link ya sirf ID — dono se ID nikaal lo."""
    text = (text or "").strip()
    if not text:
        return ""
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    return text.split("?")[0].strip("/")


def _list_children(service, folder_id, only_folders=False):
    """Ek folder ke andar ki cheezein. Drive ek baar me 100 hi deta hai, isliye loop."""
    q = f"'{folder_id}' in parents and trashed = false"
    if only_folders:
        q += f" and mimeType = '{MIME_FOLDER}'"
    out, token = [], None
    while True:
        resp = (service.files().list(
            q=q, fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)",
            pageSize=200, pageToken=token,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute())
        out.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return out


def check_access(service, root_folder_id):
    """Folder khulta hai ya nahi — sync se pehle saaf message ke liye."""
    try:
        meta = (service.files().get(fileId=root_folder_id, fields="id, name, mimeType",
                                    supportsAllDrives=True).execute())
    except Exception as exc:
        if "404" in str(exc) or "notFound" in str(exc):
            raise DriveError(
                "Ye folder nahi mila. Do wajah ho sakti hain: link galat hai, ya folder "
                "service account ke email se share nahi kiya gaya."
            )
        raise DriveError(f"Drive se baat nahi ho payi: {exc}")
    if meta.get("mimeType") != MIME_FOLDER:
        raise DriveError("Ye link kisi file ka hai, folder ka nahi.")
    return meta


def parse_sample_filename(filename, canonical_size, size_re, size_dims, normalize_code):
    """'GME01(31x15x5).jpeg' se code aur size nikaalo.

    canonical_size/size_re/size_dims/normalize_code po_module se aate hain —
    ye module usko import nahi karta taaki circular import na bane.

    Har maal size se nahi pehchana jaata — tape aur blister code se chalte
    hain. Isliye size na mile toh file chhodni nahi chahiye: dims khaali
    rehte hain aur code ke baad ka likha hua uska naam ban jaata hai.

    Lautata hai (code, dims, raw_size_text, rest_text), ya None agar code hi
    na mile.
    """
    stem = os.path.splitext(filename or "")[0]
    cm = CODE_AT_START_RE.match(stem)
    if not cm:
        return None
    code = normalize_code(cm.group("code"))
    rest = stem[cm.end():]
    m = size_re.search(rest)
    dims = size_dims(m) if m else []
    raw_size = m.group(0).strip() if m else ""
    # Size wala hissa nikaal ke jo bacha wo maal ka naam hai — "TP01 - 2 inch
    # clear" me "2 inch clear".
    leftover = (rest[:m.start()] + rest[m.end():]) if m else rest
    return code, dims, raw_size, leftover.strip(" -_()[]·,")


def download_file(service, file_id, max_bytes=12 * 1024 * 1024):
    """File ke bytes lao. Bahut badi file ho toh chhod do — DB me photo hi jaati hai."""
    from googleapiclient.http import MediaIoBaseDownload
    buf = io.BytesIO()
    req = service.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buf, req, chunksize=1024 * 1024)
    done = False
    while not done:
        _, done = downloader.next_chunk()
        if buf.tell() > max_bytes:
            raise DriveError("file too big")
    return buf.getvalue()


def list_party_folders(service, root_folder_id):
    """Main folder ke andar ke party folders — naam ke hisaab se."""
    folders = _list_children(service, root_folder_id, only_folders=True)
    return sorted(folders, key=lambda f: f["name"].lower())


def list_sample_files(service, folder_id):
    """Ek party folder ki images. Baaki files (PDF, doc) chhod di jaati hain."""
    files = _list_children(service, folder_id)
    return [f for f in files
            if f.get("mimeType") in IMAGE_MIMES
            or (f.get("mimeType") or "").startswith("image/")]
