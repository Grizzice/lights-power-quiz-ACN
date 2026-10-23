# Lights & Power Quiz

A live quiz that works like Kahoot. You show the host screen on a projector or in a Teams screen share. Players join from their phones or laptops with a game PIN. There are 15 questions with 10 seconds to answer each. Players score up to 1,000 points for each correct answer, and faster answers score more. There is a scoreboard after each question and a podium at the end.

## Put it online (one-time setup, about 15 minutes, free)

You can't use a tunnel from your Accenture laptop because Accenture's web filter blocks it. Instead, the quiz runs on Render, a free cloud host that the Accenture network allows. You need a free GitHub account and a free Render account.

1. **GitHub.** Go to github.com and sign in, then click **New repository**.
   - Name it `lights-power-quiz` and choose **Private**, so nobody can look up the answers in `quiz.json`.
   - Click **Create repository**.
2. **Upload the files.** On the new repository page, click **uploading an existing file**.
   - Open this folder, select everything in it (`public`, `vendor`, `server.py`, `quiz.json`, `render.yaml`, `requirements.txt`, and the rest) and drag it onto the page.
   - Click **Commit changes**.
3. **Render.** Go to render.com and choose **Sign up with GitHub**.
   - Click **New +**, then **Blueprint**. Pick the `lights-power-quiz` repository and click **Apply**.
   - If Blueprint isn't offered, click **New +**, then **Web Service**, and pick the repository. Set Language to **Python 3**, Build Command to `pip install -r requirements.txt`, Start Command to `python server.py` and Instance Type to **Free**.
4. After about 2 minutes the service shows **Live**, with a link like `https://lights-power-quiz.onrender.com`.

## On the day

1. About **2 minutes before** the session, open `https://<your-link>/host` on the laptop you present from. The free tier sleeps when nobody uses it, and the first visit takes up to a minute to wake it.
2. The host screen shows **Join at**, a **Game PIN** and a **QR code**. Players scan the code, or go to the link and type the PIN.
3. Click **Start** when everyone's name is on screen. Click a name to remove a player. You can also press **Space** or **Enter** instead of clicking the buttons.
4. After each question, click **Next** to see the scoreboard, then **Next** again for the next question. Click **Skip** to end a question early.
5. At the podium you can click **Download results** for a spreadsheet of every player's points, **Play again** with the same players, or **New game** for a fresh PIN.

Each time you open `/host?new`, you get a new game with a new PIN. Several games can run at the same time.

Use the 🔊 and ⛶ buttons in the bottom bar to turn the sound on or off and to go full screen.

## Rehearse first

Double-click **Rehearse on this laptop.bat**. The host screen opens in your browser. To play against yourself, open `http://localhost:8000` in a few more tabs. Each tab counts as a separate player.

## Change the questions

Edit `quiz.json` and put any new pictures in `public/img`. Each question looks like this:

```json
{ "q": "Question text", "image": "q01.jpg",
  "answers": ["Red", "Blue", "Yellow", "Green"], "correct": [0] }
```

`correct` counts from 0, so 0 is red, 1 is blue, 2 is yellow and 3 is green. You can list more than one correct answer. You can also set `"seconds": 20` on a single question. To put the change online, upload the edited file to the GitHub repository again. Render then redeploys the quiz by itself.

## Good to know

- The free Render tier comfortably handles about 100 players. For a much bigger group, switch the service to Render's Starter plan.
- If a phone locks or a player refreshes the page, they rejoin automatically with their score. If they close the tab, they can type the same nickname again after 30 seconds and keep their score.
- Games are kept in memory. If Render restarts the service mid-game, everyone has to rejoin, so avoid editing the files during a session.
