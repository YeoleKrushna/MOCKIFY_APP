# Mockify

Mockify is a Flask-based AI mock test platform where users can generate topic-based MCQ tests, attempt them in a timed exam view, and review detailed results.

## Features

- User registration and login
- Admin dashboard
- AI-generated 10-question MCQ mocks
- Separate dashboard, exam, and result pages
- Result history
- Per-user daily mock limits
- Admin-only limit management
- Retry, cooldown, caching, and fallback handling for AI generation

## Tech Stack

- Backend: Flask, SQLAlchemy, Werkzeug
- Frontend: HTML, CSS, JavaScript
- Database: SQLite
- AI provider: Groq API
- Auth: Flask session-based auth

## Project Structure

```text
Mockify/
+-- app.py
+-- auth.py
+-- admin.py
+-- mock.py
+-- results.py
+-- database.py
+-- index.html
+-- exam.html
+-- result.html
+-- requirements.txt
+-- .env.example
+-- .gitignore
+-- README.md
```

## Run Locally

1. Create and activate a virtual environment
2. Install dependencies
3. Add your `.env`
4. Start the app

```bash
pip install -r requirements.txt
python app.py
```

Open:

```text
http://127.0.0.1:5000/
```

## Main Pages

- `/` -> auth + dashboard
- `/index.html` -> auth + dashboard
- `/exam.html` -> exam page
- `/result.html` -> result page

## API Routes

### Auth

- `POST /api/auth/register`
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/auth/me`

### Mock

- `POST /api/mock/generate`
- `GET /api/mock/<mock_id>`
- `GET /api/mock/history`

### Results

- `POST /api/results/submit`
- `GET /api/results/<result_id>`
- `GET /api/results/history`

### Admin

- `GET /api/admin/stats`
- `GET /api/admin/users`
- `PUT /api/admin/users/<id>/limit`
- `DELETE /api/admin/users/<id>`
- `GET /api/admin/mocks`
- `GET /api/admin/results`

