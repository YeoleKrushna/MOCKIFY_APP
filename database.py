from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    daily_mock_limit = db.Column(db.Integer, default=5)
    mocks_taken_today = db.Column(db.Integer, default=0)
    last_reset_date = db.Column(db.Date, default=date.today)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    mocks = db.relationship('Mock', backref='user', lazy=True)
    results = db.relationship('Result', backref='user', lazy=True)

    def reset_daily_count_if_needed(self):
        today = date.today()
        if self.last_reset_date != today:
            self.mocks_taken_today = 0
            self.last_reset_date = today
            db.session.commit()

    def can_take_mock(self):
        self.reset_daily_count_if_needed()
        return self.mocks_taken_today < self.daily_mock_limit

    def to_dict(self):
        can_take_mock = self.can_take_mock()
        return {
            'id': self.id,
            'name': self.name,
            'email': self.email,
            'is_admin': self.is_admin,
            'daily_mock_limit': self.daily_mock_limit,
            'mocks_taken_today': self.mocks_taken_today,
            'mocks_remaining': max(0, self.daily_mock_limit - self.mocks_taken_today),
            'can_take_mock': can_take_mock,
            'created_at': self.created_at.isoformat()
        }


class Mock(db.Model):
    __tablename__ = 'mocks'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    topic = db.Column(db.String(500), nullable=False)
    questions = db.Column(db.Text, nullable=False)  # JSON string
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    results = db.relationship('Result', backref='mock', lazy=True)

    def to_dict(self):
        import json
        return {
            'id': self.id,
            'user_id': self.user_id,
            'topic': self.topic,
            'questions': json.loads(self.questions),
            'created_at': self.created_at.isoformat()
        }


class Result(db.Model):
    __tablename__ = 'results'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    mock_id = db.Column(db.Integer, db.ForeignKey('mocks.id'), nullable=False)
    score = db.Column(db.Integer, nullable=False)
    total = db.Column(db.Integer, default=10)
    correct_answers = db.Column(db.Integer, nullable=False)
    wrong_answers = db.Column(db.Integer, nullable=False)
    user_answers = db.Column(db.Text, nullable=False)  # JSON string
    time_taken = db.Column(db.Integer, default=0)  # seconds
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        import json
        return {
            'id': self.id,
            'user_id': self.user_id,
            'mock_id': self.mock_id,
            'score': self.score,
            'total': self.total,
            'correct_answers': self.correct_answers,
            'wrong_answers': self.wrong_answers,
            'user_answers': json.loads(self.user_answers),
            'time_taken': self.time_taken,
            'timestamp': self.timestamp.isoformat(),
            'percentage': round((self.score / self.total) * 100, 1)
        }


def init_db():
    db.create_all()
    # Create default admin if none exists
    from werkzeug.security import generate_password_hash
    admin = User.query.filter_by(is_admin=True).first()
    if not admin:
        admin = User(
            name='Admin',
            email='admin@mockify.com',
            password_hash=generate_password_hash('admin123'),
            is_admin=True,
            daily_mock_limit=999
        )
        db.session.add(admin)
        db.session.commit()
        print("Default admin created: admin@mockify.com / admin123")
