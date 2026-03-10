#!/bin/sh
exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
```

Commit it.

---

## Then Update Railway Start Command

Go to Railway → your service → **Settings → Start Command** → change it to:
```
sh start.sh
