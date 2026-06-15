-- Tenant seed: bootstrap config only. People, teams, channels, etc. are populated
-- dynamically by the tenant onboarding wizard and ongoing interviews — never
-- hardcoded in seed files (the system is multi-tenant and goes to many companies).

INSERT INTO system_config (key, value, notes) VALUES
    ('current_phase',           '0', 'Rollout phase: 0=just created, 1=mapping, 2=managers, 3=staff'),
    ('proactive_mode_enabled',  '0', 'Whether the proactive cadence loop is running'),
    ('messaging_platform',      '',  'Set by bootstrap wizard: discord | google-chat'),
    ('home_channel_platform_id','',  'Set by bootstrap wizard'),
    ('ceo_person_id',           '',  'Set by bootstrap wizard after CEO row is inserted into people');
