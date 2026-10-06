# Optional cloud workspace

Public builds ship with cloud access disabled and no project URL, publishable key,
user identity, or account password. Local work does not require Supabase.

To opt in:

1. Create or choose your own Supabase project and enable email authentication.
2. Review and run `supabase-init.sql` in that project's SQL Editor. It creates the
   workspace table and revision-checked save function. Its policies derive the
   owner from `auth.uid()`; there is no preselected owner or default account.
3. In the app's Cloud Workspace panel, enter the project's HTTPS origin and its
   publishable key or legacy `anon` key. Server `secret` and `service_role` keys
   are rejected. Enable the project and save. The desktop stores the complete
   configuration in the OS-backed encrypted store; the key is not read back into
   the renderer or written to ordinary preferences.
4. Check the connection when ready, then register or log in to your project's
   email account. Logging in starts automatic backup of workspace records and
   managed media. An empty local workspace can be restored from that account.

Saving configuration does not probe or contact the cloud project. A fresh launch
requires a new cloud login before syncing; passwords and session tokens stay in
memory. Disconnect to stop that account's syncing, or turn off cloud and save to
stop cloud access for the app. Changing the project clears active sessions and
prevents the old project's revision metadata from being reused for a new project.
Configuration changes are rejected while a cloud request is still finishing;
retry after that request completes. Existing cloud data is not deleted.

For a deliberately standalone backend, the explicit startup environment is
`IGAC_CLOUD_ENABLED=true`, `IGAC_SUPABASE_URL`, and
`IGAC_SUPABASE_PUBLISHABLE_KEY`. Omit the enable flag to remain offline, even if a
URL and key are supplied. The desktop process instead supplies its verified
secure configuration and overrides inherited cloud environment values.

Cloud publication, deployment, real account sign-in, backup, and restore require
separate operational verification. Offline source and mock tests do not validate
a Supabase deployment or the OS key store on a target computer.
