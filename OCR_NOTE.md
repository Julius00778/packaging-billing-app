# Photo/PDF se order padhna — kya chahiye

App do system tool maangta hai. Ye pip se nahi aate:

| tool | kis liye |
|---|---|
| `tesseract-ocr` | photo me se text |
| `poppler-utils` | PDF ka text layer (`pdftotext`), aur scan wale PDF ko tasveer banana (`pdftoppm`) |

Ye na hon toh app **phir bhi chalta hai** — sirf "Read from photo / PDF"
wala button saaf-saaf mana kar deta hai, aur photo form ke saath dikhti
rehti hai taaki dekh kar khud bhara ja sake.

## Railway pe

Railway ka default builder ab **Railpack** hai, **Nixpacks nahi**. Isliye
`nixpacks.toml` padha hi nahi jaata — chup-chaap nazarandaz ho jaata hai
aur build safal dikhta hai. Ye galti do baar hui, aur pakdi tab gayi jab
service ke Console me `which tesseract` chalaya:

    root@...:/app# which tesseract pdftotext pdftoppm
    bash: tesseract: command not found

Isliye config `railpack.json` me hai. `deploy.aptPackages` ka matlab hai
ki ye package chalne wale container me rahenge (sirf build ke waqt nahi).

Builder badalna ho toh: service → Settings → Build → Builder.

## Jaanchne ka tareeka

Service ke Console me:

    which tesseract pdftotext pdftoppm

Teeno ka rasta dikhna chahiye. Ya app me: koi bhi photo lagao — agar
"not switched on" wala sandesh aata hai toh package nahi aaye.
