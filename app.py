from flask import Flask, render_template, send_from_directory, abort, url_for, jsonify, request, redirect, session
import os
import re
import base64
from PyPDF2 import PdfReader
import shutil
import json
import hmac
import uuid
from functools import wraps

app = Flask(__name__, static_folder='static', template_folder='templates')
# templates are edited frequently during development; reload on every request
app.config['TEMPLATES_AUTO_RELOAD'] = True

app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'change-this-secret-in-production')

ADMIN_TOKEN_ENV = 'PORTFOLIO_ADMIN_TOKEN'
ADMIN_SESSION_KEY = 'portfolio_admin'
CERTIFICATES_SUBDIR = 'certificates'
ALLOWED_CERTIFICATE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'pdf'}

# data file for editable profile
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
PROFILE_PATH = os.path.join(DATA_DIR, 'profile.json')


def default_profile():
    return {
        "name": "",
        "tagline": "",
        "about": "",
        "contact": {"email": ""},
        "experiences": [],
        "projects": [],
        "skills": [],
        "certificates": []
    }


def _get_admin_token():
    return os.environ.get(ADMIN_TOKEN_ENV, '').strip()


def _is_local_request():
    forwarded_for = (request.headers.get('X-Forwarded-For') or '').strip()
    client_ip = forwarded_for.split(',')[0].strip() if forwarded_for else (request.remote_addr or '')
    return client_ip in ('127.0.0.1', '::1')


def _is_admin_authenticated():
    return bool(session.get(ADMIN_SESSION_KEY))


def _try_authenticate_from_request():
    configured_token = _get_admin_token()
    if not configured_token:
        return False

    submitted_token = (
        request.args.get('token')
        or request.form.get('token')
        or request.headers.get('X-Admin-Token')
        or ''
    ).strip()

    if submitted_token and hmac.compare_digest(submitted_token, configured_token):
        session[ADMIN_SESSION_KEY] = True
        return True
    return False


def can_access_admin():
    configured_token = _get_admin_token()
    if configured_token:
        if _is_admin_authenticated():
            return True
        if _try_authenticate_from_request():
            return True
        return False
    # If no admin token is configured, only local machine can edit.
    return _is_local_request()


def admin_required(view_fn):
    @wraps(view_fn)
    def wrapped(*args, **kwargs):
        if can_access_admin():
            return view_fn(*args, **kwargs)
        # Hide admin endpoints from public users.
        abort(404)

    return wrapped


def certificates_dir_path():
    return os.path.join(app.static_folder, CERTIFICATES_SUBDIR)


def ensure_certificates_dir():
    cert_dir = certificates_dir_path()
    if not os.path.exists(cert_dir):
        os.makedirs(cert_dir)
    return cert_dir


def _sanitize_name(value):
    cleaned = re.sub(r'[^A-Za-z0-9_-]+', '_', value or '').strip('_')
    return cleaned or 'certificate'


def _title_from_filename(file_name):
    stem = os.path.splitext(os.path.basename(file_name))[0]
    title = re.sub(r'[_-]+', ' ', stem).strip()
    return title or 'Certificate'


def normalize_certificate_item(raw_item):
    if isinstance(raw_item, str):
        raw_item = {"file": raw_item}

    if not isinstance(raw_item, dict):
        return None

    file_name = os.path.basename(str(raw_item.get("file", "") or "").strip())
    if not file_name:
        return None

    ext = file_name.rsplit('.', 1)[-1].lower() if '.' in file_name else ''
    if ext not in ALLOWED_CERTIFICATE_EXTENSIONS:
        return None

    title = str(raw_item.get("title", "") or "").strip() or _title_from_filename(file_name)
    return {
        "title": title,
        "file": file_name
    }


def _certificate_url(file_name):
    return f"/static/{CERTIFICATES_SUBDIR}/{file_name}"


def process_certificates_from_request(form_data, files_data, previous_certificates):
    has_submission = ("certificate_list_mode" in form_data) or bool(files_data.getlist("certificate_files"))
    if not has_submission:
        return None

    cert_dir = ensure_certificates_dir()
    next_certificates = []
    seen_files = set()

    existing_files = form_data.getlist("certificate_existing_file[]")
    existing_titles = form_data.getlist("certificate_existing_title[]")
    for idx, file_name in enumerate(existing_files):
        title = existing_titles[idx] if idx < len(existing_titles) else ""
        item = normalize_certificate_item({"file": file_name, "title": title})
        if not item:
            continue
        if item["file"] in seen_files:
            continue
        # Keep only files that actually exist on disk.
        if not os.path.exists(os.path.join(cert_dir, item["file"])):
            continue
        next_certificates.append(item)
        seen_files.add(item["file"])

    uploaded_files = files_data.getlist("certificate_files")
    for uploaded in uploaded_files:
        if not uploaded or not uploaded.filename:
            continue

        original_name = os.path.basename(uploaded.filename)
        ext = original_name.rsplit('.', 1)[-1].lower() if '.' in original_name else ''
        if ext not in ALLOWED_CERTIFICATE_EXTENSIONS:
            continue

        base_name = _sanitize_name(os.path.splitext(original_name)[0])
        unique_name = f"{base_name}_{uuid.uuid4().hex[:8]}.{ext}"
        dest_path = os.path.join(cert_dir, unique_name)
        try:
            uploaded.save(dest_path)
        except Exception:
            continue

        item = {
            "title": _title_from_filename(original_name),
            "file": unique_name
        }
        next_certificates.append(item)
        seen_files.add(item["file"])

    previous_files = set()
    for raw_item in previous_certificates or []:
        item = normalize_certificate_item(raw_item)
        if item:
            previous_files.add(item["file"])

    next_files = {item["file"] for item in next_certificates}
    files_to_remove = previous_files - next_files
    for file_name in files_to_remove:
        path = os.path.join(cert_dir, os.path.basename(file_name))
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

    return next_certificates


def normalize_experience_item(raw_item):
    if not isinstance(raw_item, dict):
        return None

    summary = str(raw_item.get("summary", "") or "").strip()
    from_date = str(raw_item.get("from", "") or "").strip()
    to_date = str(raw_item.get("to", "") or "").strip()
    if not (summary or from_date or to_date):
        return None

    return {
        "summary": summary,
        "from": from_date,
        "to": to_date
    }


def extract_experiences_from_form(form_data):
    has_list_fields = any(k in form_data for k in ("experience_list_mode", "experience_summary[]", "experience_from[]", "experience_to[]"))
    if has_list_fields:
        summaries = form_data.getlist("experience_summary[]")
        from_dates = form_data.getlist("experience_from[]")
        to_dates = form_data.getlist("experience_to[]")
        total = max(len(summaries), len(from_dates), len(to_dates))
        experiences = []
        for i in range(total):
            item = normalize_experience_item({
                "summary": summaries[i] if i < len(summaries) else "",
                "from": from_dates[i] if i < len(from_dates) else "",
                "to": to_dates[i] if i < len(to_dates) else ""
            })
            if item:
                experiences.append(item)
        return experiences

    has_legacy_fields = any(k in form_data for k in ("experience_summary", "experience_from", "experience_to"))
    if has_legacy_fields:
        item = normalize_experience_item({
            "summary": form_data.get("experience_summary", ""),
            "from": form_data.get("experience_from", ""),
            "to": form_data.get("experience_to", "")
        })
        return [item] if item else []

    return None


def normalize_project_item(raw_item):
    if isinstance(raw_item, str):
        raw_item = {"description": raw_item}

    if not isinstance(raw_item, dict):
        return None

    title = str(raw_item.get("title", "") or "").strip()
    link = str(raw_item.get("link", "") or "").strip()
    description = str(raw_item.get("description", raw_item.get("summary", "")) or "").strip()
    if not (title or link or description):
        return None

    return {
        "title": title,
        "link": link,
        "description": description
    }


def extract_projects_from_form(form_data):
    has_list_fields = any(k in form_data for k in ("project_list_mode", "project_title[]", "project_link[]", "project_description[]"))
    if has_list_fields:
        titles = form_data.getlist("project_title[]")
        links = form_data.getlist("project_link[]")
        descriptions = form_data.getlist("project_description[]")
        total = max(len(titles), len(links), len(descriptions))
        projects = []
        for i in range(total):
            item = normalize_project_item({
                "title": titles[i] if i < len(titles) else "",
                "link": links[i] if i < len(links) else "",
                "description": descriptions[i] if i < len(descriptions) else ""
            })
            if item:
                projects.append(item)
        return projects

    has_legacy_fields = any(k in form_data for k in ("project_title", "project_link", "project_description", "project_summary"))
    if has_legacy_fields:
        item = normalize_project_item({
            "title": form_data.get("project_title", ""),
            "link": form_data.get("project_link", ""),
            "description": form_data.get("project_description", form_data.get("project_summary", ""))
        })
        return [item] if item else []

    return None


def normalize_skill_item(raw_item):
    if isinstance(raw_item, str):
        raw_item = {"name": raw_item}

    if not isinstance(raw_item, dict):
        return None

    name = str(raw_item.get("name", raw_item.get("title", "")) or "").strip()
    level = str(raw_item.get("level", "") or "").strip()
    details = str(raw_item.get("details", raw_item.get("description", "")) or "").strip()
    if not (name or level or details):
        return None

    return {
        "name": name,
        "level": level,
        "details": details
    }


def extract_skills_from_form(form_data):
    has_list_fields = any(k in form_data for k in ("skill_list_mode", "skill_name[]", "skill_level[]", "skill_details[]"))
    if has_list_fields:
        names = form_data.getlist("skill_name[]")
        levels = form_data.getlist("skill_level[]")
        details_list = form_data.getlist("skill_details[]")
        total = max(len(names), len(levels), len(details_list))
        skills = []
        for i in range(total):
            item = normalize_skill_item({
                "name": names[i] if i < len(names) else "",
                "level": levels[i] if i < len(levels) else "",
                "details": details_list[i] if i < len(details_list) else ""
            })
            if item:
                skills.append(item)
        return skills

    has_legacy_fields = any(k in form_data for k in ("skill_name", "skill_level", "skill_details"))
    if has_legacy_fields:
        item = normalize_skill_item({
            "name": form_data.get("skill_name", ""),
            "level": form_data.get("skill_level", ""),
            "details": form_data.get("skill_details", "")
        })
        return [item] if item else []

    return None


def load_profile_data():
    profile = default_profile()
    try:
        with open(PROFILE_PATH, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            profile["name"] = str(raw.get("name", "") or "").strip()
            profile["tagline"] = str(raw.get("tagline", "") or "").strip()
            profile["about"] = str(raw.get("about", "") or "").strip()

            raw_contact = raw.get("contact") if isinstance(raw.get("contact"), dict) else {}
            profile["contact"]["email"] = str(raw_contact.get("email", "") or "").strip()

            experiences = []
            raw_experiences = raw.get("experiences")
            if isinstance(raw_experiences, list):
                for raw_item in raw_experiences:
                    item = normalize_experience_item(raw_item)
                    if item:
                        experiences.append(item)

            # Backward compatibility with the old single-experience shape.
            if not experiences and isinstance(raw.get("experience"), dict):
                legacy_item = normalize_experience_item(raw.get("experience"))
                if legacy_item:
                    experiences.append(legacy_item)

            profile["experiences"] = experiences

            projects = []
            raw_projects = raw.get("projects")
            if isinstance(raw_projects, list):
                for raw_item in raw_projects:
                    item = normalize_project_item(raw_item)
                    if item:
                        projects.append(item)
            elif isinstance(raw_projects, (dict, str)):
                single_item = normalize_project_item(raw_projects)
                if single_item:
                    projects.append(single_item)

            # Backward compatibility with possible old single-project shape.
            if not projects and isinstance(raw.get("project"), dict):
                legacy_project = normalize_project_item(raw.get("project"))
                if legacy_project:
                    projects.append(legacy_project)

            profile["projects"] = projects

            skills = []
            raw_skills = raw.get("skills")
            if isinstance(raw_skills, list):
                for raw_item in raw_skills:
                    item = normalize_skill_item(raw_item)
                    if item:
                        skills.append(item)
            elif isinstance(raw_skills, dict):
                single_item = normalize_skill_item(raw_skills)
                if single_item:
                    skills.append(single_item)
            elif isinstance(raw_skills, str):
                if "," in raw_skills:
                    for part in raw_skills.split(","):
                        item = normalize_skill_item(part)
                        if item:
                            skills.append(item)
                else:
                    single_item = normalize_skill_item(raw_skills)
                    if single_item:
                        skills.append(single_item)

            # Backward compatibility with possible old single-skill shape.
            if not skills and isinstance(raw.get("skill"), dict):
                legacy_skill = normalize_skill_item(raw.get("skill"))
                if legacy_skill:
                    skills.append(legacy_skill)

            profile["skills"] = skills

            certificates = []
            raw_certificates = raw.get("certificates")
            if isinstance(raw_certificates, list):
                for raw_item in raw_certificates:
                    item = normalize_certificate_item(raw_item)
                    cert_path = os.path.join(certificates_dir_path(), item["file"]) if item else ""
                    if item and os.path.exists(cert_path):
                        item["url"] = _certificate_url(item["file"])
                        certificates.append(item)
            elif isinstance(raw_certificates, (dict, str)):
                single_item = normalize_certificate_item(raw_certificates)
                cert_path = os.path.join(certificates_dir_path(), single_item["file"]) if single_item else ""
                if single_item and os.path.exists(cert_path):
                    single_item["url"] = _certificate_url(single_item["file"])
                    certificates.append(single_item)

            # Backward compatibility with possible old single-certificate shape.
            if not certificates and raw.get("certificate") is not None:
                legacy_item = normalize_certificate_item(raw.get("certificate"))
                cert_path = os.path.join(certificates_dir_path(), legacy_item["file"]) if legacy_item else ""
                if legacy_item and os.path.exists(cert_path):
                    legacy_item["url"] = _certificate_url(legacy_item["file"])
                    certificates.append(legacy_item)

            profile["certificates"] = certificates
    except Exception:
        pass
    return profile


def ensure_data_dir():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)


def ensure_resume_in_static():
    """Ensure a resume PDF exists at static/resume.pdf. If not, copy any PDF from project root into static."""
    static_resume = os.path.join(app.static_folder, 'resume.pdf')
    if os.path.exists(static_resume):
        return static_resume
    root = os.path.dirname(os.path.abspath(__file__))
    pdfs = [f for f in os.listdir(root) if f.lower().endswith('.pdf')]
    if pdfs:
        src = os.path.join(root, pdfs[0])
        try:
            shutil.copy2(src, static_resume)
            return static_resume
        except Exception:
            return None
    return None


def get_profile_image_filename():
    """Return a filename in the static folder for the profile image if present, else default to profile.svg"""
    static_files = os.listdir(app.static_folder)
    candidates = [f for f in static_files if f.startswith('profile.') and f.split('.')[-1].lower() in ('png', 'jpg', 'jpeg', 'svg', 'webp')]
    if candidates:
        # prefer non-svg if available
        for ext in ('png', 'jpg', 'jpeg', 'webp'):
            for c in candidates:
                if c.lower().endswith(ext):
                    return c
        return candidates[0]
    return 'profile.svg'


def get_profile_image_version(file_name):
    image_path = os.path.join(app.static_folder, os.path.basename(file_name or ''))
    try:
        return str(os.stat(image_path).st_mtime_ns)
    except Exception:
        try:
            return str(int(os.path.getmtime(image_path) * 1_000_000_000))
        except Exception:
            return '0'


def extract_text_from_pdf(path):
    try:
        reader = PdfReader(path)
        text = []
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text.append(page_text)
        return "\n".join(text)
    except Exception:
        return ""


def score_resume(text):
    # Basic ATS-friendly heuristics
    score = 0
    details = {}

    # Contact info
    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    phone_match = re.search(r"\+?\d[\d\s\-()]{7,}\d", text)
    details['has_email'] = bool(email_match)
    details['has_phone'] = bool(phone_match)
    score += 15 if details['has_email'] else 0
    score += 10 if details['has_phone'] else 0

    # Sections
    sections = ['experience', 'education', 'skills', 'projects', 'summary', 'contact']
    found_sections = [s for s in sections if s in text.lower()]
    details['found_sections'] = found_sections
    score += min(30, 6 * len(found_sections))

    # Length (words)
    words = re.findall(r"\w+", text)
    wc = len(words)
    details['word_count'] = wc
    if wc >= 400:
        score += 20
    elif wc >= 200:
        score += 12
    else:
        score += 6

    # Keywords (sample tech keywords)
    keywords = ['python', 'flask', 'django', 'aws', 'azure', 'sql', 'javascript', 'react', 'docker', 'kubernetes', 'git']
    matches = [k for k in keywords if k in text.lower()]
    details['keywords_found'] = matches
    score += min(25, 5 * len(matches))

    # Normalize to 0-100
    final = int(min(100, score))
    details['score'] = final
    return details


@app.route('/')
def index():
    ensure_data_dir()
    resume_exists = os.path.exists(os.path.join(app.static_folder, 'resume.pdf'))
    profile = load_profile_data()
    profile_image = get_profile_image_filename()
    profile_image_version = get_profile_image_version(profile_image)
    can_edit = can_access_admin()
    # flag used to display a small confirmation message after saving
    saved_flag = request.args.get('saved') == '1'
    return render_template(
        'home.html',
        resume_exists=resume_exists,
        profile=profile,
        profile_image=profile_image,
        profile_image_version=profile_image_version,
        can_edit=can_edit,
        saved=saved_flag
    )


@app.route('/resume')
def resume():
    resume_path = os.path.join(app.static_folder, 'resume.pdf')
    if os.path.exists(resume_path):
        return send_from_directory(app.static_folder, 'resume.pdf', as_attachment=True)
    # fallback: look for a PDF in project root and serve it
    root = os.path.dirname(os.path.abspath(__file__))
    pdfs = [f for f in os.listdir(root) if f.lower().endswith('.pdf')]
    if pdfs:
        return send_from_directory(root, pdfs[0], as_attachment=True)
    abort(404)


@app.route('/view')
def view_resume():
    # ensure resume is available in static (copy fallback if needed)
    resume_path = ensure_resume_in_static()
    if resume_path and os.path.exists(resume_path):
        return redirect(url_for('static', filename='resume.pdf'))
    abort(404)


@app.route('/score')
def score():
    # ensure resume is available in static
    resume_path = ensure_resume_in_static()
    if not resume_path or not os.path.exists(resume_path):
        return jsonify({'error': 'resume not found'}), 404
    text = extract_text_from_pdf(resume_path)
    details = score_resume(text)
    return jsonify(details)


@app.route('/upload_resume', methods=['POST'])
@admin_required
def upload_resume():
    file = request.files.get('resume')
    if not file:
        return redirect('/')
    filename = 'resume.pdf'
    dest = os.path.join(app.static_folder, filename)
    try:
        file.save(dest)
    except Exception:
        pass
    return redirect(url_for('view_resume'))


@app.route('/upload_image', methods=['POST'])
@admin_required
def upload_image():
    file = request.files.get('image')
    if not file:
        return redirect('/')
    fname = file.filename or ''
    ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
    if ext not in ('png', 'jpg', 'jpeg', 'svg', 'webp'):
        return redirect('/admin')
    dest_name = f'profile.{ext}'
    dest = os.path.join(app.static_folder, dest_name)
    # remove existing profile.* files to avoid duplicates
    for f in os.listdir(app.static_folder):
        if f.startswith('profile.'):
            try:
                os.remove(os.path.join(app.static_folder, f))
            except Exception:
                pass
    try:
        file.save(dest)
    except Exception:
        pass
    # If AJAX request, return JSON with URL (add cache-busting param)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        import time
        url = url_for('static', filename=dest_name) + '?v=' + str(int(time.time()))
        return jsonify({'success': True, 'url': url})
    return redirect('/')


@app.route('/save_profile', methods=['POST'])
@admin_required
def save_profile():
    # request imported at module level
    ensure_data_dir()
    profile = load_profile_data()
    profile['name'] = request.form.get('name', profile['name']).strip()
    profile['tagline'] = request.form.get('tagline', profile['tagline']).strip()
    profile['about'] = request.form.get('about', profile['about']).strip()
    profile['contact']['email'] = request.form.get('email', profile['contact']['email']).strip()
    new_experiences = extract_experiences_from_form(request.form)
    if new_experiences is not None:
        profile['experiences'] = new_experiences
    new_projects = extract_projects_from_form(request.form)
    if new_projects is not None:
        profile['projects'] = new_projects
    new_skills = extract_skills_from_form(request.form)
    if new_skills is not None:
        profile['skills'] = new_skills
    new_certificates = process_certificates_from_request(
        request.form,
        request.files,
        profile.get('certificates', [])
    )
    if new_certificates is not None:
        profile['certificates'] = new_certificates
    try:
        with open(PROFILE_PATH, 'w', encoding='utf-8') as f:
            json.dump(profile, f, indent=2)
    except Exception:
        pass
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True})
    return redirect('/')


@app.route('/edit')
@admin_required
def edit():
    ensure_data_dir()
    profile = load_profile_data()
    profile_image = get_profile_image_filename()
    profile_image_version = get_profile_image_version(profile_image)
    return render_template(
        'edit.html',
        profile=profile,
        profile_image=profile_image,
        profile_image_version=profile_image_version
    )


@app.route('/save_profile_full', methods=['POST'])
@admin_required
def save_profile_full():
    # Accept multipart form with optional image file and profile fields
    ensure_data_dir()
    profile = load_profile_data()
    profile['name'] = request.form.get('name', profile['name']).strip()
    profile['tagline'] = request.form.get('tagline', profile['tagline']).strip()
    profile['about'] = request.form.get('about', profile['about']).strip()
    profile['contact']['email'] = request.form.get('email', profile['contact']['email']).strip()
    new_experiences = extract_experiences_from_form(request.form)
    if new_experiences is not None:
        profile['experiences'] = new_experiences
    new_projects = extract_projects_from_form(request.form)
    if new_projects is not None:
        profile['projects'] = new_projects
    new_skills = extract_skills_from_form(request.form)
    if new_skills is not None:
        profile['skills'] = new_skills
    new_certificates = process_certificates_from_request(
        request.form,
        request.files,
        profile.get('certificates', [])
    )
    if new_certificates is not None:
        profile['certificates'] = new_certificates
    try:
        with open(PROFILE_PATH, 'w', encoding='utf-8') as f:
            json.dump(profile, f, indent=2)
    except Exception:
        pass

    def remove_existing_profile_images():
        for f in os.listdir(app.static_folder):
            if f.startswith('profile.'):
                try:
                    os.remove(os.path.join(app.static_folder, f))
                except Exception:
                    pass

    # Preferred: cropped image from client-side editor (data URL)
    cropped_data = request.form.get('image_cropped_data', '').strip()
    saved_cropped = False
    if cropped_data:
        m = re.match(r'^data:image/(png|jpe?g|jpg|webp);base64,(.+)$', cropped_data, re.IGNORECASE)
        if m:
            ext = m.group(1).lower()
            if ext == 'jpeg':
                ext = 'jpg'
            payload = (m.group(2) or '').strip()
            try:
                raw = base64.b64decode(payload, validate=True)
            except Exception:
                try:
                    raw = base64.b64decode(payload.replace(' ', '+'))
                except Exception:
                    raw = b''
            # Ignore suspiciously large payloads
            if raw and len(raw) <= 15 * 1024 * 1024:
                dest_name = f'profile.{ext}'
                dest = os.path.join(app.static_folder, dest_name)
                temp_dest = os.path.join(app.static_folder, f'.{dest_name}.tmp')
                try:
                    with open(temp_dest, 'wb') as out:
                        out.write(raw)
                    remove_existing_profile_images()
                    os.replace(temp_dest, dest)
                    saved_cropped = True
                except Exception:
                    try:
                        if os.path.exists(temp_dest):
                            os.remove(temp_dest)
                    except Exception:
                        pass
                    pass

    # Fallback: raw file upload
    image_saved = False
    saved_name = None

    if not saved_cropped:
        file = request.files.get('image')
        if file:
            fname = file.filename or ''
            ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
            if ext in ('png', 'jpg', 'jpeg', 'svg', 'webp'):
                dest_name = f'profile.{ext}'
                dest = os.path.join(app.static_folder, dest_name)
                remove_existing_profile_images()
                try:
                    file.save(dest)
                    image_saved = True
                    saved_name = dest_name
                except Exception:
                    pass

    # if the image was modified (cropped or file uploaded) we want to force a fresh
    # copy on the next page load; appending the version to the redirect URL guarantees
    # the browser won't show a cached copy.
    redirect_url = '/'
    params = []
    if image_saved or saved_cropped:
        version = None
        if saved_name:
            version = get_profile_image_version(saved_name)
        else:
            # profile_image may have changed during cropped processing, recompute
            profile_image = get_profile_image_filename()
            version = get_profile_image_version(profile_image)
        params.append('v=' + (version or '0'))
    # always let the index template know we saved something
    params.append('saved=1')
    if params:
        redirect_url = redirect_url + '?' + '&'.join(params)

    return redirect(redirect_url)


if __name__ == '__main__':
    app.run(debug=True)
