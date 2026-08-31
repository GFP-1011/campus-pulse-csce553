# M1 Baseline System — Evidence

Application URL: `https://YOUR-APP.onrender.com`

Repository URL: `https://github.com/YOUR-ACCOUNT/YOUR-REPOSITORY`

## Logged-in UI

Replace this text with a screenshot showing Campus Pulse while logged in as one of the demo users. The screenshot should include the feed or another dynamic-data operation.

`![Campus Pulse logged-in UI](images/logged-in-ui.png)`

## API evidence against the hosted service

Paste the terminal output for the README curl examples below. Keep the command and response, but redact the bearer token before committing this file.

### Health

```text
$ curl -i https://YOUR-APP.onrender.com/health
PASTE OUTPUT HERE
```

### Login

```text
$ curl ... /api/auth/login
PASTE OUTPUT HERE; REDACT access_token
```

### Write: create message

```text
$ curl ... -X POST /api/messages
PASTE OUTPUT HERE
```

### Read: list messages

```text
$ curl ... /api/messages
PASTE OUTPUT HERE
```

All users and content shown here are fake. This is a CSCE 553 class baseline, not a production service.

