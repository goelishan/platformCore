# Tag for humans, digest for the build. python:3.12-slim is reassigned to a new
# patch release every few weeks; without the digest, two builds of the same commit
# can sit on different interpreters. The tag records the intent, 3.12 slim, and is
# ignored at pull time in favour of the digest.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

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