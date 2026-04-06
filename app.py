from flask import Flask, send_from_directory
from flask_cors import CORS
from database import db, init_db
from dotenv import load_dotenv
import os

# Load variables from .env before any route/module accesses os.environ.
# override=True ensures stale shell vars do not mask the saved .env values.
load_dotenv(override=True)

try:
    from routes.auth import auth_bp
    from routes.mock import mock_bp
    from routes.admin import admin_bp
    from routes.results import results_bp
except ModuleNotFoundError:
    # Current workspace stores route blueprints at repository root.
    from auth import auth_bp
    from mock import mock_bp
    from admin import admin_bp
    from results import results_bp

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'mockify-secret-key-change-in-prod')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///mockify.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

db.init_app(app)

app.register_blueprint(auth_bp, url_prefix='/api/auth')
app.register_blueprint(mock_bp, url_prefix='/api/mock')
app.register_blueprint(admin_bp, url_prefix='/api/admin')
app.register_blueprint(results_bp, url_prefix='/api/results')


@app.route('/')
def serve_index():
    return send_from_directory(os.getcwd(), 'index.html')


@app.route('/index.html')
def serve_index_html():
    return send_from_directory(os.getcwd(), 'index.html')


@app.route('/result.html')
def serve_result_html():
    return send_from_directory(os.getcwd(), 'result.html')


@app.route('/exam.html')
def serve_exam_html():
    return send_from_directory(os.getcwd(), 'exam.html')

with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
