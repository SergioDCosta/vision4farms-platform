from pathlib import Path
from decouple import config
import os

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("SECRET_KEY")
DEBUG = config("DEBUG", default=True, cast=bool)

ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="127.0.0.1,localhost").split(",")

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "cloudinary_storage",
    "cloudinary",
    "django_htmx",
    "rest_framework",
    "channels",

    "apps.accounts",
    "apps.catalog",
    "apps.inventory",
    "apps.marketplace",
    "apps.recommendations",
    "apps.orders",
    "apps.alerts",
    "apps.messaging",
    "apps.dashboard",
    "apps.settings_app",
    "apps.notifications_app",
    "apps.integrations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "apps.common.middleware.SessionUserMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [config("REDIS_URL", default="redis://127.0.0.1:6379/0")],
        },
    }
}

CSRF_TRUSTED_ORIGINS = config(
    "CSRF_TRUSTED_ORIGINS",
    default="http://127.0.0.1:8000"
).split(",")

SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("DB_NAME"),
        "USER": config("DB_USER"),
        "PASSWORD": config("DB_PASSWORD"),
        "HOST": config("DB_HOST", default="127.0.0.1"),
        "PORT": config("DB_PORT", default="5432"),
    }
}

LANGUAGE_CODE = "pt-pt"
TIME_ZONE = "Europe/Lisbon"
USE_I18N = True
USE_TZ = True

STORAGES = {
    "default": {
        "BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

CLOUDINARY_STORAGE = {
    "CLOUD_NAME": config("CLOUDINARY_CLOUD_NAME"),
    "API_KEY": config("CLOUDINARY_API_KEY"),
    "API_SECRET": config("CLOUDINARY_API_SECRET"),
}

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/login/"
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"

# Email / SMTP real
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = config("EMAIL_HOST")
EMAIL_PORT = config("EMAIL_PORT", cast=int, default=465)

EMAIL_USE_SSL = config("EMAIL_USE_SSL", cast=bool, default=True)
EMAIL_USE_TLS = config("EMAIL_USE_TLS", cast=bool, default=False)

EMAIL_HOST_USER = config("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD")

DEFAULT_FROM_EMAIL = config(
    "DEFAULT_FROM_EMAIL",
    default="VISION4FARMS <no-reply@farm.vision4you.pt>",
)

DEFAULT_REPLY_TO_EMAIL = config(
    "DEFAULT_REPLY_TO_EMAIL",
    default="support@farm.vision4you.pt",
)

SERVER_EMAIL = DEFAULT_FROM_EMAIL
EMAIL_TIMEOUT = 20
