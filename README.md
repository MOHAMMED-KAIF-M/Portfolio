Portfolio Website (Flask)
=========================

This is a small Flask portfolio that includes an embedded resume viewer and a basic ATS-friendly resume scorer.

Setup
-----

1. Create and activate a virtual environment (Windows PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

2. Place your resume PDF at `static/resume.pdf` (use the attached PDF or copy your file there).

Run
----

```powershell
python app.py
```

Deploy (Render - Recommended)
-----------------------------

This project is a Flask backend app, so deploy it as a Python web service (not Netlify static hosting).

1. Push this repo to GitHub.
2. In Render, choose **New +** -> **Blueprint**.
3. Select this repository (`render.yaml` is already included).
4. Deploy.
5. In Render service settings, keep environment variables set:
   - `FLASK_SECRET_KEY`
   - `PORTFOLIO_ADMIN_TOKEN`

Start command used by Render:

```bash
gunicorn app:app
```

Important storage note:
- On free/ephemeral hosting, uploaded images/certificates and edited profile data can reset on restart/redeploy.
- For persistent edits, use persistent disk/database/object storage.

Endpoints
---------
- `/` : Home
- `/view` : Embedded PDF viewer
- `/resume` : Download resume PDF
- `/score` : Run a basic ATS-friendly score and view breakdown

Notes
-----
- The UI can be swapped to use a specific Gemini UI library if you provide a link; currently it uses the included CSS which is lightweight and responsive.
- The ATS scorer uses simple heuristics (contact info, common sections, keywords, length). It is not a replacement for a commercial ATS but useful for quick checks.
