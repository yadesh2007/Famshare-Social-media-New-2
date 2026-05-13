# FamShare Chat

A chat-only Flask DBMS project with user registration, login, private conversations, and real-time messaging through Flask-SocketIO.

## Features
- User registration and login
- Chat list with all available users
- Private conversations
- Real-time message delivery
- SQLite persistence

## Requirements
- Python 3.10+
- Flask
- Flask-SocketIO
- SQLite

## Install
```bash
pip install -r requirements.txt
```

## Run
```bash
python app.py
```

Open `http://127.0.0.1:5000`.

## Chat-only Mode
Chat-only mode is enabled by default with `CHAT_ONLY_MODE=1`. Set `CHAT_ONLY_MODE=0` only if you want to re-enable the older social routes still present in `app.py`.
