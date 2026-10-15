release: python scripts/heroku_release.py
web: python -m uvicorn server:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips '*'
