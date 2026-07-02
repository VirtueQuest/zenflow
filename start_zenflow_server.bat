@echo off
echo Installing ZenFlow dependencies for Windows...
echo.

REM Upgrade pip first
python -m pip install --upgrade pip

REM Install packages one group at a time so failures are isolated

echo [1/8] Core framework...
pip install fastapi==0.115.0 "uvicorn[standard]==0.30.6"

echo [2/8] Security...
pip install "python-jose[cryptography]==3.3.0" "passlib[bcrypt]==1.7.4" python-multipart==0.0.9 slowapi==0.1.9 python-dotenv==1.0.1

echo [3/8] Validation...
pip install pydantic==2.8.2 email-validator==2.2.0

echo [4/8] Database (pre-built Windows wheels)...
pip install asyncpg==0.29.0
REM If asyncpg fails on Python 3.13, run: pip install asyncpg --pre
pip install "psycopg[binary]==3.2.3"

echo [5/8] Cache + Scheduler...
pip install "redis[hiredis]==5.0.8" apscheduler==3.10.4

echo [6/8] Notifications + Storage...
pip install twilio==9.3.3 sendgrid==6.11.0 httpx==0.27.2 boto3==1.35.0 aiofiles==24.1.0

echo [7/8] Payments...
pip install stripe==10.12.0

echo [8/8] Metrics...
pip install prometheus-client==0.21.0

echo.
echo Done! Run: uvicorn main:app --reload --port 8000
uvicorn main:app --reload --port 8000
