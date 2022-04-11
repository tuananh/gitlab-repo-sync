FROM python:3-alpine
LABEL maintainer "Tuan Anh Tran <me@tuananh.org>"

WORKDIR /usr/src/app

RUN apk update && \
    apk upgrade && \
    apk add --no-cache git

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY sync.py .

ENTRYPOINT [ "python", "./sync.py" ]
CMD [ "--help" ]