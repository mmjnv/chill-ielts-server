# Chill IELTS teacher server

This is your private task bank, student-code system, chart uploader, and answer inbox. It uses a small local SQLite database, so no Google Sheet or separate database is needed.

## Start it on your computer

Open Terminal, then run this command (replace the password with your own):

```sh
cd "/Users/ism/Documents/Codex/2026-07-17/i-want/outputs/chill-ielts-server"
/Users/ism/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 server.py
```

The first run asks you to create the teacher-dashboard password. Then open:

- Teacher dashboard: `http://localhost:8080/admin`
- Student test page: `http://localhost:8080/`

## Teacher workflow

1. Sign in at `/admin`.
2. Create a test, type both questions, and upload the Task 1 chart.
3. Press **Generate student code**. Give that code to one student.
4. Read their submitted answers in **Submissions**.

## Optional AI writing feedback

The dashboard now includes an **AI mark** button beside every submission. It gives an *unofficial classroom estimate* with feedback, strengths, and improvements; you remain the final teacher and marker.

1. Create an OpenAI API key in your OpenAI developer account. Do not send the key to students or paste it into the website.
2. Before starting the server, run this in Terminal, replacing the example text with your own key:

```sh
export OPENAI_API_KEY='your-key-goes-here'
export OPENAI_MODEL='gpt-5.4-mini'
/Users/ism/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 server.py
```

3. Open **Submissions** and select **AI mark** for a completed response.

The key stays only in the Terminal session and server process. It is never sent to the student page or stored in this project.

## Put it online safely

This is secure enough for a private computer or a properly configured HTTPS host. Do not expose it directly from your home network. Deploy the whole `outputs` folder to a managed Python host, configure a strong `ADMIN_PASSWORD` and `SESSION_SECRET` as private environment variables, and enable HTTPS. Keep the generated `data/settings.json` and database out of any public repository.

For an actual public school service, the next upgrade should be teacher accounts, student names/logins, rate limiting, backups, and HTTPS from the hosting provider.
