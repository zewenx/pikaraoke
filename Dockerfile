FROM python:3.9

USER root

WORKDIR /usr/src/app
RUN apt update
RUN apt install -y ffmpeg libsndfile1 vlc

ARG VLC_UID=1000
ARG VLC_GID=1000
RUN groupadd -g "${VLC_GID}" home && \
    useradd -m -d /home/pi -s /bin/sh -u "${VLC_UID}" -g "${VLC_GID}" pi

COPY --chown=pi . .
RUN pip install --upgrade pip
RUN pip install youtube-dl

RUN pip install --no-cache-dir -r requirements.txt

USER pi
CMD [ "python3", "./app.py" ]