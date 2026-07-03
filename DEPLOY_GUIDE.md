# Deploy Guide — Apna Billing App Online Kaise Lagayein (Step-by-Step)

Ye guide bilkul simple rakhi gayi hai, bina coding background ke bhi follow kar sakte hain.
Total time: ~30-40 minutes (pehli baar).

Hum **Railway.app** use karenge — wahan "server rent" karne ka matlab sirf signup karna
hai, Linux/coding setup nahi karna padta.

---

## Step 1 — Code GitHub pe daalo (5 min)

GitHub ek free jagah hai jahan code store hota hai — Railway wahan se code uthata hai.

1. https://github.com pe free account banayein (agar nahi hai).
2. Login karne ke baad, top-right pe **+** button → **New repository**.
3. Repository name dein jaise `packaging-billing-app`. **Private** select karein
   (kyunki ismein aapki firm ki settings/code hai). **Create repository** click karein.
4. Ab us repository page pe "uploading an existing file" wala link milega — click karein.
5. Is poore project folder (`packaging-erp`) ke andar ke saare files aur folders
   (`app.py`, `models.py`, `templates/`, `static/`, `requirements.txt`, `Procfile`,
   `.gitignore`) ko drag-and-drop kar dein GitHub ke upload box mein.
6. Niche "Commit changes" button click karein.

> Agar drag-and-drop se dikkat aaye (folders ka), to GitHub Desktop app
> (https://desktop.github.com) install karke wahan se bhi upload kar sakte hain — wo
> thoda aasan hota hai folders ke liye.

## Step 2 — Railway pe account banayein (2 min)

1. https://railway.app pe jayein → **Login with GitHub** se signup karein (same
   GitHub account jo Step 1 mein use kiya).
2. Ek payment plan choose karna hoga taaki app 24x7 chalta rahe — **Hobby plan**
   (~$5/month minimum, usage ke hisab se thoda zyada bhi ho sakta hai) zyada chhoti
   businesses ke liye kaafi hai. Card details dene honge.

## Step 3 — App deploy karein (5 min)

1. Railway dashboard pe **New Project** click karein.
2. **Deploy from GitHub repo** select karein → apni `packaging-billing-app`
   repository choose karein.
3. Railway khud detect kar lega ki ye Python/Flask app hai aur deploy shuru kar
   dega. 2-3 minute wait karein.
4. Deploy complete hone ke baad, apni service pe click karein → **Settings** tab →
   **Networking** section → **Generate Domain** click karein. Ab aapko ek link
   milega jaisे `your-app-name.up.railway.app` — **yahi link hai jo aap kahin se
   bhi browser mein khol ke app use kar sakte hain.**

## Step 4 — Database connect karein (5 min) — zaroori for multi-location use

Bina is step ke, app chalega lekin data redeploy hone par delete ho sakta hai. Ye
step data ko permanently safe rakhta hai.

1. Railway project canvas mein **New** → **Database** → **Add PostgreSQL** click
   karein.
2. Railway khud `DATABASE_URL` naam ka environment variable aapki app service mein
   daal dega — kuch aur karne ki zarurat nahi, app khud usse pakad lega (already
   code mein set hai).
3. Apni web service pe wapas jayein → **Deployments** tab → latest deployment ko
   **Redeploy** kar dein, taaki naya database connect ho jaye.

## Step 5 — Security ke liye Secret Key set karein (2 min)

1. Web service → **Variables** tab → **New Variable** add karein:
   - Name: `SECRET_KEY`
   - Value: koi bhi random 20-30 character ka text (jaise `xK9pL2mN8qR5tY7wA3cF6hJ1`)
2. Save karein, service apne aap redeploy ho jayegi.

## Step 6 — Pehla Owner account banayein (2 min)

1. Apna app link (`your-app-name.up.railway.app`) browser mein kholein.
2. Aapko automatically **Setup** page pe le jaaya jayega — apna naam, username,
   password dalke pehla **Owner** account banayein.
3. Login karein — ab Settings mein jaake firm details, GSTIN, bank details, state
   bharein, aur Users mein jaake staff ke liye login bana dein.

## Roz ka use

- Office/factory mein ho ya kahin bhi — bas apna app link kisi bhi computer/phone
  ke browser mein kholke login karein.
- Link ko bookmark kar lein ya WhatsApp pe staff ko bhej dein.

## Agar kuch atak jaye

- Railway project ke andar **Deployments → View Logs** mein error dikh jayega —
  wo error message copy karke mujhe (Claude ko) bhej dein, main samjha dunga.
- Galti se data clear karna ho to Postgres database ko Railway dashboard se reset
  kar sakte hain (Owner ko hi karna chahiye, aur ye permanent hai).

---

**Note:** Railway ki pricing aur exact UI thodi change ho sakti hai time ke saath —
agar koi step screen pe match na ho, to mujhe screenshot ya description bhej dein,
main updated steps de dunga.
