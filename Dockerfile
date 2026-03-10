FROM python:3.11-slim

WORKDIR /app

# Install CPU-only PyTorch first (much smaller than default)
RUN pip install --no-cache-dir \
    torch==2.5.1+cpu \
    --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## Fix 2 — Remove torch from requirements.txt

Go to GitHub → `requirements.txt` → pencil icon → find and delete this line:
```
torch==2.5.1
