import asyncio
from base64 import urlsafe_b64encode
from datetime import datetime, timezone, timedelta
from os import urandom
from pathlib import Path

from flask import Flask, abort, redirect, render_template, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import FlaskForm
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_all_lexers, get_lexer_by_name
from sqlalchemy import Text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from wtforms import SelectField, TextAreaField
from wtforms.validators import DataRequired
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import os

load_dotenv()

def external_url_for(endpoint, **values):
    url = url_for(endpoint, _external=True, **values)
    # 替换为自定义域名
    return url.replace('127.0.0.1:5000', os.environ.get("SERVER_NAME"))

app = Flask(__name__)
app.config["SECRET_KEY"] = "top secret!"
app.config["SQLALCHEMY_DATABASE_URI"] = (
    f"sqlite+aiosqlite:///{Path(__file__).parent / 'db.sqlite'}"
)
app.jinja_env.globals['external_url_for'] = external_url_for
db = SQLAlchemy(app)
scheduler = BackgroundScheduler()

from flask import url_for

class PasteForm(FlaskForm):
    body = TextAreaField("Body", validators=[DataRequired()])
    language = SelectField("Language")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.language.choices = sorted(
            [(lexer[1][0], lexer[0]) for lexer in get_all_lexers() if lexer[1]]
        )


class Paste(db.Model):
    id: Mapped[str] = mapped_column(
        primary_key=True, default=lambda: urlsafe_b64encode(urandom(4)).decode()[:6]
    )
    body: Mapped[str] = mapped_column(Text)
    language: Mapped[str]
    timestamp: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc), index=True
    )
    deleted: Mapped[bool] = mapped_column(default=False)


with app.app_context():
    # Create database
    async def initdb():
        engine = create_async_engine(db.engine.url)
        db.Session = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as con:
            await con.run_sync(db.Model.metadata.create_all)

    asyncio.run(initdb())


async def cleanup_old_pastes():
    with app.app_context():
        async with db.Session() as session:
            # let deleted prop turn true if pastes older than 1 hour
            await session.execute(
                db.update(Paste)
                .where(Paste.timestamp < datetime.now() - timedelta(minutes=10))
                .values(deleted=True)
            )
            await session.commit()


def sync_cleanup_old_pastes():
    asyncio.run(cleanup_old_pastes())


scheduler.add_job(func=sync_cleanup_old_pastes, trigger="interval", minutes=10)
scheduler.start()


@app.route("/", methods=["GET", "POST"])
async def index():
    async with db.Session() as session:
        form = PasteForm()
        if form.validate_on_submit():
            paste = Paste(body=form.body.data, language=form.language.data)
            session.add(paste)
            await session.commit()
            return redirect(url_for("paste", id=paste.id))
        # query = db.select(Paste).order_by(Paste.timestamp.desc()).limit(10)
        query = (
            db.select(Paste)
            .order_by(Paste.timestamp.desc())
            .filter(Paste.deleted == False)
            .limit(10)
        )
        pastes = [paste async for paste in await session.stream_scalars(query)]
        return render_template("index.html", form=form, pastes=pastes)


@app.get("/<id>")
async def paste(id):
    async with db.Session() as session:
        # paste = await session.get(Paste, id)
        # search by id and check if it's not deleted
        paste = (
            await session.execute(db.select(Paste).filter_by(id=id, deleted=False))
        ).scalar_one_or_none()
        if not paste:
            abort(404)
        lexer = get_lexer_by_name(paste.language, stripall=True)
        formatter = HtmlFormatter(lineos=True, cssclass="source")
        highlight_body = highlight(paste.body, lexer, formatter)
        highlight_css = formatter.get_style_defs(".source")
        app.logger.info(f"Paste {id} viewed")
        app.logger.info(f"paste: {paste}")
        return render_template(
            "paste.html",
            paste=paste,
            highlight_body=highlight_body,
            highlight_css=highlight_css,
        )


@app.errorhandler(404)
async def not_found(e):
    return render_template("404.html"), 404
