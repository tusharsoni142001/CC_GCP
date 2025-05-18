FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py github_analyzer.py CustomException.py github_release_analyzer.py utils.py ./

RUN touch .env

ENV PORT=8080

CMD ["python3", "app.py"]
