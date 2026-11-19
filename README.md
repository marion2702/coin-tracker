# Coin Tracker

Toutes les 5 min via GitHub Actions, alertes sur Telegram :

- **tracker.py** : suit BLUEALIEN, KIWI et JETDOG (prix, market cap, volume, gros holders). Réglages : `config.json`.
- **scanner.py** : repère les coins pump.fun qui démarrent fort (volume, acheteurs qui accélèrent, achats > ventes) et écarte les signaux d'arnaque évidents. Réglages : `scanner_config.json`. Historique des alertes : `scanner_alerts.csv`.

Ce n'est pas un conseil d'investissement : la plupart des coins signalés retombent.
