# Action Lab

A dependency-free local prototype for the task “Test action.” The supplied `references/` directory was empty, so there were no additional requirements or acceptance criteria to implement.

Open `index.html` in a modern browser. No installation, server, network access, or deployment is needed.

Enter an action name, select a simulated success or failure, and choose **Run test**. The app shows a running state followed by the result and records the latest 20 runs in browser local storage. **Clear history** removes those saved runs. If storage is unavailable, the app keeps history in memory for the current session.

Manual checks:
- Run a successful action and verify the passed result and history entry.
- Run a failed action and verify the failed result and history entry.
- Repeated clicks while running must produce only one history entry.
- Blank and whitespace-only names must be rejected.
- Refresh and verify saved history remains; clear history and refresh to verify removal.
- Use a name containing HTML and verify it displays as plain text.

This is a simulation; it does not execute arbitrary code or call external services.
