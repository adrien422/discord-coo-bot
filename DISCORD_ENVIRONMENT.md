# Discord COO Environment

- Guild ID: 1499169248402997379
- Home channel: coo-cockpit (1499180162057769020)
- Watched channel count: 23
- Admin command channels: 1499181679980445797, 1499181681779802112, 1499181682941493299

## Categories

- COO Administration: 1499181678319374358
- COO Control: 1499180160421986377
- Cockpits: 1499181686238347514
- Departments: 1499180166478303312
- Employee Inbox: 1499180177379430581
- Strategic Staff: 1499181670438277250

## Text Channels

- #accounting: 1499180170035335311
- #blockers: 1499180180088819832
- #coo-admin: 1499181679980445797
- #coo-audit: 1499181682941493299
- #coo-cockpit: 1499180162057769020
- #coo-config: 1499181681779802112
- #coo-decisions: 1499180163026653245
- #coo-escalations: 1499180164091871292
- #coo-pulses: 1499180165115281551
- #department-cockpit: 1499181689774145598
- #employee-notes: 1499180178633654343
- #executive-cockpit: 1499181687479734384
- #finance-strategy: 1499181675001675959
- #handoffs: 1499180180894257255
- #leadership: 1499181673000992849
- #manager-cockpit: 1499181688956260492
- #operations: 1499180168848216128
- #people: 1499180176175534321
- #sales: 1499180171620515917
- #strategy: 1499181671839170794
- #support: 1499180173021679778
- #tech: 1499180174430830767

## Operating Notes

- General department and inbox channels accept employee messages; unsolicited messages are saved to the reference inbox.
- Locked Strategic Staff, COO Administration, and Cockpits rooms are for higher-trust work.
- Admin COO controls are restricted to coo-admin, coo-config, and coo-audit.
- Human messages reach the COO agent only when they reply to a COO bot message; admin rooms additionally require mentioning the bot.
- Daily per-channel transcripts live under reference/transcripts and include clear sender names.
- Saved reference messages keep classification tags in the files themselves, while workflow state is tracked separately as pending, queued, held, no-action, initiated, or failed.
- The inbox monitor periodically queues pending/failed reference messages for COO attention.
- `/coo cockpit` opens the Discord-native cockpit panel. Admin buttons appear only in admin rooms.
- `/coo queue` shows browsable workflow state, open follow-ups, and automation lanes; `/coo review` immediately queues pending inbox messages for attention.
- `/coo facts` shows the current room's weekly/monthly factsheets; `/coo updatefacts` queues an agent refresh for those factsheets.
- Use Discord messages for workplace output; the tmux Codex/Claude pane remains headless backend state.
