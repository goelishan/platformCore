FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

# --require-hashes fails the build when a downloaded artifact does not match the
# digest in the lock, which is the only point in the pipeline where a tampered or
# re-uploaded package can still be caught. It also refuses a file that is not fully
# pinned, so the lock cannot decay into a partial one without the build saying so.
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]