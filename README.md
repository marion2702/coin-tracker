# Coin Tracker

Toutes les 2 min via GitHub Actions (une boucle qui se relance seule toutes les ~5 h 30), alertes sur Telegram :

- **tracker.py** : suit BLUEALIEN, KIWI et JETDOG (prix, market cap, volume, gros holders). Réglages : `config.json`.
- **scanner.py** : repère les coins pump.fun qui démarrent fort (volume, acheteurs qui accélèrent, achats > ventes) et écarte les signaux d'arnaque évidents. Réglages : `scanner_config.json`. Historique des alertes : `scanner_alerts.csv`.

- **paper_trader.py** : trading en **simulation** (aucun vrai SOL, aucune clé privée). Achète fictivement 0,1 SOL de chaque coin signalé par le scanner, revend la moitié à +50 % et le reste à +100 %. Deux stratégies tournent en parallèle sur les mêmes coins pour comparer :
  - **A_rapide** : coupe à -25 % ou après 30 min ;
  - **B_patient** : coupe à -50 % ou après 4 h (laisse le temps aux coins qui baissent puis remontent).
  Max 3 positions par stratégie, arrêt après -0,5 SOL dans la journée. Coûts simulés : 3,5 % par achat et par vente (frais + slippage). Réglages : `paper_config.json`. Journal : `paper_trades.csv`. Bilan : `python3 paper_trader.py --report`.
  Limite : les prix ne sont vérifiés qu'à chaque passage GitHub (~2 min), donc un stop-loss peut sortir bien plus bas que -25 %.

Ce n'est pas un conseil d'investissement : la plupart des coins signalés retombent.
