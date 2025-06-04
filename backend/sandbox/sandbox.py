import os
import subprocess
import shlex
from pathlib import Path

from daytona_sdk import Daytona, DaytonaConfig, CreateSandboxParams, Sandbox, SessionExecuteRequest
from daytona_api_client.models.workspace_state import WorkspaceState
from dotenv import load_dotenv
from utils.logger import logger
from utils.config import config
from utils.config import Configuration

load_dotenv()

# Determine if we should use Daytona or fall back to a local Docker sandbox
_daytona_api_key = os.getenv("DAYTONA_API_KEY", "").strip()
_use_daytona = bool(_daytona_api_key)

if _use_daytona:
    logger.debug("Initializing Daytona sandbox configuration")
    daytona_config = DaytonaConfig(
        api_key=config.DAYTONA_API_KEY,
        server_url=config.DAYTONA_SERVER_URL,
        target=config.DAYTONA_TARGET
    )

    if daytona_config.api_key:
        logger.debug("Daytona API key configured successfully")
    else:
        logger.warning("No Daytona API key found in environment variables")

    if daytona_config.server_url:
        logger.debug(f"Daytona server URL set to: {daytona_config.server_url}")
    else:
        logger.warning("No Daytona server URL found in environment variables")

    if daytona_config.target:
        logger.debug(f"Daytona target set to: {daytona_config.target}")
    else:
        logger.warning("No Daytona target found in environment variables")

    daytona = Daytona(daytona_config)
    logger.debug("Daytona client initialized")
else:
    daytona_config = None
    daytona = None
    logger.warning(
        "DAYTONA_API_KEY not provided - using local docker-compose sandbox"
    )

    _compose_file = Path(__file__).parent / "docker" / "docker-compose.yml"
    _local_sandbox = None

    class LocalProcess:
        """Minimal process management using docker exec and tmux."""

        def __init__(self, container_id: str):
            self.container_id = container_id

        def exec(self, command: str, timeout: int = 60):
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    self.container_id,
                    "bash",
                    "-lc",
                    command,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return type(
                "ExecResult",
                (),
                {
                    "exit_code": result.returncode,
                    "result": result.stdout + result.stderr,
                },
            )()

        def create_session(self, session_id: str):
            subprocess.run(
                [
                    "docker",
                    "exec",
                    self.container_id,
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    session_id,
                ],
                check=False,
            )

        def delete_session(self, session_id: str):
            subprocess.run(
                [
                    "docker",
                    "exec",
                    self.container_id,
                    "tmux",
                    "kill-session",
                    "-t",
                    session_id,
                ],
                check=False,
            )

        def execute_session_command(self, session_id: str, req: SessionExecuteRequest, var_async: bool = True, **_):
            cmd = req.command if hasattr(req, "command") else str(req)
            subprocess.run(
                [
                    "docker",
                    "exec",
                    self.container_id,
                    "tmux",
                    "send-keys",
                    "-t",
                    session_id,
                    cmd,
                    "Enter",
                ],
                check=False,
            )
            return type(
                "CmdResult",
                (),
                {"cmd_id": cmd, "exit_code": 0},
            )()

        def get_session_command_logs(self, session_id: str, command_id: str = None):
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    self.container_id,
                    "tmux",
                    "capture-pane",
                    "-t",
                    session_id,
                    "-p",
                    "-S",
                    "-",
                    "-E",
                    "-",
                ],
                capture_output=True,
                text=True,
            )
            return result.stdout

    class LocalFS:
        """Minimal FS wrapper using docker exec."""

        def __init__(self, container_id: str):
            self.container_id = container_id

        def create_folder(self, path: str, permissions: str = "755"):
            subprocess.run(
                [
                    "docker",
                    "exec",
                    self.container_id,
                    "mkdir",
                    "-p",
                    path,
                ],
                check=False,
            )
            subprocess.run(
                [
                    "docker",
                    "exec",
                    self.container_id,
                    "chmod",
                    permissions,
                    path,
                ],
                check=False,
            )

        def upload_file(self, path: str, data: bytes):
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    self.container_id,
                    "bash",
                    "-c",
                    f"cat > {shlex.quote(path)}",
                ],
                input=data,
                text=False,
            )

        def download_file(self, path: str):
            result = subprocess.run(
                ["docker", "exec", self.container_id, "cat", path],
                capture_output=True,
            )
            return result.stdout

        def delete_file(self, path: str):
            subprocess.run(
                ["docker", "exec", self.container_id, "rm", "-f", path],
                check=False,
            )

        def set_file_permissions(self, path: str, permissions: str):
            subprocess.run(
                ["docker", "exec", self.container_id, "chmod", permissions, path],
                check=False,
            )

    class LocalSandbox:
        def __init__(self, container_id: str):
            self.id = container_id
            self.process = LocalProcess(container_id)
            self.fs = LocalFS(container_id)

        def get_preview_link(self, port: int):
            return f"http://localhost:{port}"

    def _ensure_local_sandbox() -> LocalSandbox:
        global _local_sandbox
        if _local_sandbox is None:
            subprocess.run(
                ["docker", "compose", "-f", str(_compose_file), "up", "-d"],
                check=True,
            )
            container_id = (
                subprocess.check_output(
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(_compose_file),
                        "ps",
                        "-q",
                        "kortix-suna",
                    ]
                )
                .decode()
                .strip()
            )
            _local_sandbox = LocalSandbox(container_id or "local-sandbox")
        return _local_sandbox

async def get_or_start_sandbox(sandbox_id: str):
    """Retrieve a sandbox by ID, check its state, and start it if needed."""

    logger.info(f"Getting or starting sandbox with ID: {sandbox_id}")

    if not _use_daytona:
        return _ensure_local_sandbox()

    try:
        sandbox = daytona.get_current_sandbox(sandbox_id)
        
        # Check if sandbox needs to be started
        if sandbox.instance.state == WorkspaceState.ARCHIVED or sandbox.instance.state == WorkspaceState.STOPPED:
            logger.info(f"Sandbox is in {sandbox.instance.state} state. Starting...")
            try:
                daytona.start(sandbox)
                # Wait a moment for the sandbox to initialize
                # sleep(5)
                # Refresh sandbox state after starting
                sandbox = daytona.get_current_sandbox(sandbox_id)
                
                # Start supervisord in a session when restarting
                start_supervisord_session(sandbox)
            except Exception as e:
                logger.error(f"Error starting sandbox: {e}")
                raise e
        
        logger.info(f"Sandbox {sandbox_id} is ready")
        return sandbox
        
    except Exception as e:
        logger.error(f"Error retrieving or starting sandbox: {str(e)}")
        raise e

def start_supervisord_session(sandbox: Sandbox):
    """Start supervisord in a session."""
    session_id = "supervisord-session"
    if not _use_daytona:
        # The local container already runs supervisord via its entrypoint
        return
    try:
        logger.info(f"Creating session {session_id} for supervisord")
        sandbox.process.create_session(session_id)

        # Execute supervisord command
        sandbox.process.execute_session_command(
            session_id,
            SessionExecuteRequest(
                command="exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/supervisord.conf",
                var_async=True,
            ),
        )
        logger.info(f"Supervisord started in session {session_id}")
    except Exception as e:
        logger.error(f"Error starting supervisord session: {str(e)}")
        raise e

def create_sandbox(password: str, project_id: str = None):
    """Create a new sandbox with all required services configured and running."""

    if not _use_daytona:
        return _ensure_local_sandbox()

    logger.debug("Creating new Daytona sandbox environment")
    logger.debug("Configuring sandbox with browser-use image and environment variables")
    
    labels = None
    if project_id:
        logger.debug(f"Using sandbox_id as label: {project_id}")
        labels = {'id': project_id}
        
    params = CreateSandboxParams(
        image=Configuration.SANDBOX_IMAGE_NAME,
        public=True,
        labels=labels,
        env_vars={
            "CHROME_PERSISTENT_SESSION": "true",
            "RESOLUTION": "1024x768x24",
            "RESOLUTION_WIDTH": "1024",
            "RESOLUTION_HEIGHT": "768",
            "VNC_PASSWORD": password,
            "ANONYMIZED_TELEMETRY": "false",
            "CHROME_PATH": "",
            "CHROME_USER_DATA": "",
            "CHROME_DEBUGGING_PORT": "9222",
            "CHROME_DEBUGGING_HOST": "localhost",
            "CHROME_CDP": ""
        },
        resources={
            "cpu": 2,
            "memory": 4,
            "disk": 5,
        }
    )
    
    # Create the sandbox
    sandbox = daytona.create(params)
    logger.debug(f"Sandbox created with ID: {sandbox.id}")
    
    # Start supervisord in a session for new sandbox
    start_supervisord_session(sandbox)
    
    logger.debug(f"Sandbox environment successfully initialized")
    return sandbox

async def delete_sandbox(sandbox_id: str):
    """Delete a sandbox by its ID."""
    logger.info(f"Deleting sandbox with ID: {sandbox_id}")

    if not _use_daytona:
        subprocess.run(["docker", "compose", "-f", str(_compose_file), "down"], check=False)
        globals()["_local_sandbox"] = None
        logger.info("Local sandbox stopped")
        return True

    try:
        # Get the sandbox
        sandbox = daytona.get_current_sandbox(sandbox_id)

        # Delete the sandbox
        daytona.remove(sandbox)

        logger.info(f"Successfully deleted sandbox {sandbox_id}")
        return True
    except Exception as e:
        logger.error(f"Error deleting sandbox {sandbox_id}: {str(e)}")
        raise e

