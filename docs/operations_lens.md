# Local Operations Lens & Operator Server

Piceli's operations lens is a lightweight local companion to durable deployment sessions and cluster operations. It answers essential operational questions safely: *what does this release declare it owns, what does the selected cluster currently show, and how does an operator reach an approved service?*

## Deployment Model: Where is it Deployed?

**Piceli Observe is a laptop-local operator control plane server, NOT an in-cluster Pod.**

- **Host Environment**: Runs directly on the developer or operator's workstation (`127.0.0.1:9876`) or inside a CI/CD runner execution context.
- **Cluster Connection**: Uses the caller's authorized local `kubeconfig` (e.g., `target/k-lab-p2/kubeconfig`) to query the Kubernetes API.
- **Port Forwarding Architecture**: Spawns and supervises local `kubectl port-forward` child processes that bind exclusively to loopback (`127.0.0.1`). It never discovers, adopts, or kills untracked background processes.
- **Security Boundary**: Cluster credentials, tokens, and private keys never leave the operator's machine. Untrusted code running in cluster pods has zero access to the operations server or deployment credentials.

## One-Click Forwarding Shortcuts

The operations lens includes built-in quick shortcuts for core services:

| Shortcut ID | Service Target | Local Port | Remote Port | Target URL |
| :--- | :--- | :--- | :--- | :--- |
| **`kabuki`** | `service/ih-kabuki` | `3000` | `3000` | `http://127.0.0.1:3000` |
| **`monitor`** | `service/ih-rustvello-monitor` | `18084` | `18084` | `http://127.0.0.1:18084` |
| **`poet`** | `service/ih-target-poet` | `18086` | `18080` | `http://127.0.0.1:18086` |
| **`shibuya`** | `service/ih-shibuya` | `18083` | `18083` | `http://127.0.0.1:18083` |

Operators can click **Start Forward** in the UI to immediately bridge network access, and click the direct link (`Open ↗`) to launch the web interface in their browser.

## Agent and Human Ergonomics

### For AI Agents
- **Semantic HTML & Data Test IDs**: All interactive buttons, cards, table rows, and metrics carry explicit `data-testid` attributes (e.g. `data-testid="shortcut-card-kabuki"`, `data-testid="btn-start-kabuki"`, `data-testid="link-open-kabuki"`, `data-testid="tbl-forwards"`).
- **Structured REST API**:
  - `GET /v1/shortcuts`: Current status and live URLs for all shortcuts.
  - `POST /v1/forwards/quick`: Start or stop a shortcut by ID.
  - `POST /v1/forwards/add`: Register and start a custom loopback forward.
  - `POST /v1/forwards/delete`: Stop and remove a saved forward.
  - `GET /v1/status`: Machine-readable desired vs live inventory.
  - `GET /v1/releases`: Catalogued releases, active version, and OCI digests.

### For Humans
- **Dark Mode Ergonomics**: Sleek interface with live pulse status indicators (pulsing green dot for running services).
- **Toast Notifications**: Non-blocking toast feedback for all actions (e.g. starting a port forward, promoting a release, running GC) without jarring browser `alert()` dialogs.
- **Auto-Refresh Controls**: Live 3-second polling with an instant pause toggle.

## Starting the Control Plane

```bash
# Serve live cluster operator lens:
piceli operator serve --kubeconfig target/k-lab-p2/kubeconfig --namespace infinite-haiku-p2 --port 9876

# Or via observe CLI:
piceli observe serve --kubeconfig target/k-lab-p2/kubeconfig --user default --port 9876
```
