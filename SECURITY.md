# Security

Report a vulnerability privately through GitHub's "Report a vulnerability"
(Security tab), not a public issue.

- Never commit or share a master key, a node key or an `agent.env`. The repository
  contains none; tests use throwaway keys made at run time.
- A node holds only its own key: if one node is compromised, rotate that node's
  identity by changing its name or the fleet master key, and re-provision.
- To rotate the fleet master key: set the new key on the receiver and the
  provisioner, then re-install the agent on every node (EdgeShell, Fleet Setup).
