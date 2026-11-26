import os
import sys
import subprocess
import json
import time
import zipfile
import requests
import readline
import glob
import inquirer
import shutil
import re
from urllib.parse import quote
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeRemainingColumn
from rich.prompt import Prompt
from rich.text import Text
from github import Github, Auth, UnknownObjectException
from .core.settings import CONFIG_FILE, LOG_FILE, UPSTREAM_WEB_URL
from .services.updater import (
    UpdateState,
    apply_upstream_update,
    fetch_upstream_status,
)
from .services.security import (
    add_paths_to_gitignore,
    extract_github_secret_paths,
    is_path_ignored,
    redact_sensitive_text,
    scan_for_secrets,
)
from .services.release_notes import (
    ReleasePlan,
    compare_readmes,
    read_local_readme,
    suggestion_text,
)
from .ui.menu import (
    MenuAction,
    RepositoryAction,
    select_main_action,
    select_repository,
)
from .ui.renderer import BoxRow, console, render_box
from . import __version__

# ==================== UI HELPERS ====================

def print_header(show_upstream=False, update_status=None):
    if console.is_terminal:
        console.clear()

    product_name = Text(f"GITHUB AUTO UPLOADER  PRO  v{__version__}", style="bold white", justify="center")
    product_meta = Text(
        f"Version {__version__} • Repository automation for GitHub",
        style="muted",
        justify="center",
    )
    render_box(None, [product_name, product_meta])

    if show_upstream and update_status:
        if update_status.release:
            release_title = f"UPSTREAM {update_status.release.tag}"
            release_lines = [
                BoxRow("Release", update_status.release.title, "brand"),
                BoxRow("Update Status", update_status.status_text),
                BoxRow("Release Notes", update_status.release.notes),
            ]
        else:
            release_title = "UPSTREAM STATUS"
            release_lines = [
                BoxRow("Local Revision", update_status.local_revision),
                BoxRow("Update Status", update_status.status_text),
                BoxRow("Details", update_status.error or "Release metadata is unavailable."),
            ]
        render_box(
            release_title,
            release_lines,
        )

def print_step(message):
    console.print()
    console.rule(f"[brand]{message}[/brand]", align="left", style="bright_blue")

def print_success(message):
    render_box("SUCCESS", [Text(message, style="success")], border_style="#39FF88")

def print_error(message):
    render_box(
        "ERROR",
        [Text(message, style="danger")],
        border_style="#FF5577",
        title_style="danger",
    )

# ==================== PATH COMPLETION SETUP ====================

class PathCompleter:
    """Custom tab completion for file paths"""
    def __init__(self):
        self.matches = []
    
    def complete(self, text, state):
        if state == 0:
            if text.startswith("~"):
                text = os.path.expanduser("~") + text[1:]
            
            if os.path.isdir(text):
                try:
                    items = os.listdir(text)
                    self.matches = [os.path.join(text, item) for item in items]
                except PermissionError:
                    self.matches = []
            else:
                try:
                    self.matches = glob.glob(text + "*")
                except:
                    self.matches = []
            
            for i, match in enumerate(self.matches):
                if os.path.isdir(match):
                    self.matches[i] = match + os.sep
        
        try:
            return self.matches[state]
        except IndexError:
            return None

def setup_tab_completion():
    completer = PathCompleter()
    readline.set_completer(completer.complete)
    if 'libedit' in readline.__doc__:
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    readline.set_completer_delims(" \t\n;")

# ==================== CORE FUNCTIONS ====================

def log(message):
    with open(LOG_FILE, "a") as f:
        f.write(f"[{time.ctime()}] {message}\n")

def check_git_installed():
    try:
        subprocess.run(["git", "--version"], capture_output=True, text=True, check=True)
    except:
        print_error("Git is not installed. Please install Git first.")
        sys.exit(1)

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

MAX_RECENT_REPOS = 10

def get_recent_repos(config, username):
    """Return recently used repository names for an account, newest first."""
    return list(config.get("recent_repos", {}).get(username, []))

def remember_recent_repo(config, username, repo_name):
    """Move repo_name to the top of the account's recent repository list."""
    recent = config.setdefault("recent_repos", {})
    names = [n for n in recent.get(username, []) if n.casefold() != repo_name.casefold()]
    recent[username] = [repo_name] + names[:MAX_RECENT_REPOS - 1]
    save_config(config)

def get_folder_remote_repo(folder, username):
    """Return the repo name of the folder's origin remote if it belongs to username."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=folder, capture_output=True, text=True,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?/?$", result.stdout.strip())
    if match and match.group(1).casefold() == username.casefold():
        return match.group(2)
    return None

def fetch_github_username(token):
    try:
        headers = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
        response = requests.get('https://api.github.com/user', headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json().get('login')
    except:
        pass
    return None

def masked_token_prompt(message):
    """Read a secret value while showing stars for typed or pasted characters."""
    console.print(f"{message}: ", end="")
    token_chars = []

    if os.name == 'nt':
        import msvcrt
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                console.print()
                break
            if ch == "\003":
                raise KeyboardInterrupt
            if ch in ("\b", "\x7f"):
                if token_chars:
                    token_chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            token_chars.append(ch)
            sys.stdout.write("★")
            sys.stdout.flush()
        return "".join(token_chars)

    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                console.print()
                break
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch in ("\b", "\x7f"):
                if token_chars:
                    token_chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            token_chars.append(ch)
            sys.stdout.write("★")
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    return "".join(token_chars)

def prompt_for_github_account(title="GitHub Authentication"):
    """Prompt for a token and return validated account details."""
    print_step(title)
    token = masked_token_prompt("Enter GitHub Personal Access Token")
    username = fetch_github_username(token)
    if not username:
        username = Prompt.ask("Enter your GitHub username")
    return username, token

def save_account(config, username, token):
    """Save an account and mark it active. Keeps old config keys for compatibility."""
    accounts = config.setdefault("accounts", {})
    accounts[username] = {
        "username": username,
        "token": token
    }
    config["active_account"] = username
    config["username"] = username
    config["token"] = token
    save_config(config)

def ensure_accounts_config(config):
    """Migrate older single-account config into the newer accounts format."""
    if config.get("username") and config.get("token"):
        accounts = config.setdefault("accounts", {})
        username = config["username"]
        if username not in accounts:
            accounts[username] = {
                "username": username,
                "token": config["token"]
            }
        config.setdefault("active_account", username)
        save_config(config)
    return config

def get_active_account(config):
    active_username = config.get("active_account") or config.get("username")
    accounts = config.get("accounts", {})
    account = accounts.get(active_username)
    if account and account.get("token") and account.get("username"):
        return account["username"], account["token"]
    if config.get("username") and config.get("token"):
        return config["username"], config["token"]
    return None, None

def switch_github_account(config):
    """Switch to a saved GitHub account or add a new one."""
    ensure_accounts_config(config)
    accounts = config.get("accounts", {})
    active_username = config.get("active_account") or config.get("username")

    choices = []
    for username in sorted(accounts):
        label = username
        if username == active_username:
            label = f"{username} (current)"
        choices.append(label)

    choices.extend(["➕ Add New Account", "🔙 Back to Main Menu"])
    account_q = [
        inquirer.List(
            'account',
            message="Select GitHub account",
            choices=choices,
            carousel=True
        )
    ]
    account_ans = inquirer.prompt(account_q)
    if not account_ans or "Back" in account_ans['account']:
        return get_active_account(config)

    selection = account_ans['account']
    if "Add New Account" in selection:
        username, token = prompt_for_github_account("Add GitHub Account")
        save_account(config, username, token)
        print_success(f"Switched to GitHub account: {username}")
        return username, token

    selected_username = selection.replace(" (current)", "")
    account = accounts.get(selected_username)
    if not account:
        print_error("Selected account was not found in config.")
        return get_active_account(config)

    config["active_account"] = selected_username
    config["username"] = account["username"]
    config["token"] = account["token"]
    save_config(config)
    print_success(f"Switched to GitHub account: {selected_username}")
    return account["username"], account["token"]

def auto_detect_folder(folder_name):
    """Automatically detect a folder by name in the system"""
    console.print(f"[blue]🔍 Searching for folder: '{folder_name}'...[/blue]")
    
    # Check current directory first
    if os.path.isdir(folder_name):
        return os.path.abspath(folder_name)
    
    # Search common locations based on OS
    if os.name == 'nt':
        search_roots = [f"{chr(drive)}:\\" for drive in range(ord('C'), ord('Z') + 1) if os.path.exists(f"{chr(drive)}:\\")]
    else:
        search_roots = [os.path.expanduser("~"), '/']

    for root_dir in search_roots:
        try:
            for root, dirs, _ in os.walk(root_dir):
                if folder_name in dirs:
                    found_path = os.path.join(root, folder_name)
                    console.print(f"[green]✅ Found: {found_path}[/green]")
                    return found_path
                # Limit depth for performance
                if root.count(os.sep) - root_dir.count(os.sep) > 3:
                    del dirs[:]
        except:
            continue
    return None

def run_command(command, cwd=None):
    result = subprocess.run(command, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        safe_command = redact_sensitive_text(" ".join(str(part) for part in command))
        safe_error = redact_sensitive_text(result.stderr)
        log(f"Error executing {safe_command}: {safe_error}")
    return result

def build_authenticated_repo_url(username, repo_name, token):
    safe_token = quote(token, safe="")
    return f"https://{safe_token}@github.com/{username}/{repo_name}.git"

def build_clean_repo_url(username, repo_name):
    return f"https://github.com/{username}/{repo_name}.git"

def confirm_secret_history_reset(folder, affected_paths, findings=None):
    """Explain old-history secrets and confirm a clean-history rebuild."""
    rows = []
    if findings:
        for finding in findings[:20]:
            rows.append(BoxRow(finding.kind, f"{finding.path}:{finding.line}", "danger"))
    else:
        already_ignored = [path for path in affected_paths if is_path_ignored(folder, path)]
        missing_rules = [path for path in affected_paths if path not in already_ignored]
        if already_ignored:
            rows.append(BoxRow("Already Ignored", ", ".join(already_ignored), "success"))
        if missing_rules:
            rows.append(BoxRow("Needs Ignore Rule", ", ".join(missing_rules), "danger"))
        rows.append(BoxRow(
            "Why Blocked",
            ".gitignore protects future commits; these paths still exist in older Git commits.",
            "warning",
        ))
    rows.extend([
        BoxRow("Local Files", "All files stay on this device. No project file is deleted.", "success"),
        BoxRow(
            "Clean History",
            "Only the local .git history is replaced, then current non-ignored files are committed.",
        ),
        BoxRow(
            "Credential Safety",
            "Revoke or rotate any real credential that appeared in Git history.",
            "warning",
        ),
    ])
    render_box("SECRET IN GIT HISTORY", rows, border_style="#FF5577", title_style="danger")

    choices = [
        "Cancel — keep current Git history",
        "Rebuild clean Git history — keep all local files",
    ]
    answer = inquirer.prompt([
        inquirer.List(
            "recovery",
            message="Resolve secrets stored in old Git commits",
            choices=choices,
            default=choices[0],
        )
    ])
    if not answer or answer["recovery"] != choices[1]:
        return False

    add_paths_to_gitignore(folder, affected_paths)
    git_dir = os.path.join(folder, ".git")
    if os.path.exists(git_dir):
        shutil.rmtree(git_dir)
    print_success(
        "Local files were kept. Affected paths are ignored and clean Git history will be created."
    )
    return True

def handle_gitignore(folder):
    """Detect common heavy/sensitive files and offer to create a .gitignore"""
    common_ignores = [".env", "node_modules", "__pycache__", ".venv", "venv"]
    found_to_ignore = []
    
    for item in common_ignores:
        if (
            os.path.exists(os.path.join(folder, item))
            and not is_path_ignored(folder, item)
        ):
            found_to_ignore.append(item)
            
    # Check for large files (> 50MB)
    large_files = []
    for root, dirs, files in os.walk(folder):
        # Skip directories already marked for ignore or common hidden ones
        dirs[:] = [d for d in dirs if d not in common_ignores and not d.startswith('.')]
        for file in files:
            file_path = os.path.join(root, file)
            try:
                size_mb = os.path.getsize(file_path) / (1024 * 1024)
                rel_path = os.path.relpath(file_path, folder)
                if size_mb > 50 and not is_path_ignored(folder, rel_path):
                    large_files.append((rel_path, size_mb))
            except:
                continue

    if found_to_ignore:
        gitignore_path = os.path.join(folder, ".gitignore")
        has_gitignore = os.path.exists(gitignore_path)
        console.print(
            f"\n[yellow]🔍 Missing .gitignore rules for: {', '.join(found_to_ignore)}[/yellow]"
        )
        prompt_message = (
            "Update existing .gitignore with these missing exclusions?"
            if has_gitignore
            else "Create .gitignore with these exclusions?"
        )
        questions = [
            inquirer.List('choice',
                          message=prompt_message,
                          choices=['Yes', 'No'],
                          default='Yes'),
        ]
        answer = inquirer.prompt(questions)
        if answer and answer['choice'] == "Yes":
            existing_content = ""
            if os.path.exists(gitignore_path):
                with open(gitignore_path, "r") as f:
                    existing_content = f.read()
            
            with open(gitignore_path, "a") as f:
                if not existing_content.endswith("\n") and existing_content:
                    f.write("\n")
                for item in found_to_ignore:
                    if item not in existing_content:
                        f.write(f"{item}/\n" if os.path.isdir(os.path.join(folder, item)) else f"{item}\n")
            console.print("[green]✅ .gitignore updated![/green]")

    if large_files:
        console.print(f"\n[yellow]⚠️  Detected large files (> 50MB) that might fail GitHub upload:[/yellow]")
        for rel_path, size_mb in large_files:
            color = "red" if size_mb > 100 else "yellow"
            console.print(f"   [{color}]- {rel_path} ({size_mb:.2f} MB)[/{color}]")
        
        questions = [
            inquirer.List('choice',
                          message="Do you want to add these large files to .gitignore?",
                          choices=['Yes', 'No'],
                          default='Yes'),
        ]
        answer = inquirer.prompt(questions)
        if answer and answer['choice'] == "Yes":
            gitignore_path = os.path.join(folder, ".gitignore")
            existing_content = ""
            if os.path.exists(gitignore_path):
                with open(gitignore_path, "r") as f:
                    existing_content = f.read()
            
            with open(gitignore_path, "a") as f:
                if not existing_content.endswith("\n") and existing_content:
                    f.write("\n")
                for rel_path, _ in large_files:
                    if rel_path not in existing_content:
                        f.write(f"{rel_path}\n")
            console.print("[green]✅ Large files added to .gitignore![/green]")

def setup_git_and_push(
    folder,
    authenticated_repo_url,
    clean_repo_url,
    commit_message,
    recovery_attempted=False,
):
    if not os.path.exists(folder):
        print_error(f"Folder '{folder}' does not exist!")
        return False

    handle_gitignore(folder)

    findings = scan_for_secrets(folder)
    if findings and not recovery_attempted:
        affected_paths = sorted({finding.path for finding in findings})
        if not confirm_secret_history_reset(folder, affected_paths, findings):
            print_error("Upload cancelled. No secret was pushed or bypassed.")
            return False
        recovery_attempted = True

    steps = [
        (["git", "init"], "Initializing Git..."),
        (["git", "remote", "remove", "origin"], "Resetting remote..."),
        (["git", "remote", "add", "origin", clean_repo_url], "Adding safe remote..."),
        (["git", "rm", "-r", "--cached", "."], "Cleaning index..."),
        (["git", "add", "."], "Staging files..."),
        (["git", "config", "user.email", "auto@upload.bot"], "Setting email..."),
        (["git", "config", "user.name", "GitUploaderBot"], "Setting name..."),
    ]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=console,
        transient=True
    ) as progress:
        task = progress.add_task("[cyan]Processing Git", total=len(steps) + 2)
        
        for cmd, desc in steps:
            progress.update(task, description=f"[cyan]{desc}", advance=1)
            run_command(cmd, cwd=folder)
        
        status = run_command(["git", "status", "--porcelain"], cwd=folder)
        if status.stdout.strip():
            progress.update(task, description="[cyan]Committing...", advance=0.5)
            run_command(["git", "commit", "-m", commit_message], cwd=folder)
        
        progress.update(task, description="[cyan]Pushing to GitHub...", advance=1)
        run_command(["git", "branch", "-M", "main"], cwd=folder)
        push = run_command(
            ["git", "push", authenticated_repo_url, "main:main", "--force"],
            cwd=folder,
        )
        
        if push.returncode == 0:
            return True
        else:
            progress.stop()
            is_secret_block = any(
                marker in push.stderr
                for marker in ["GH013", "Push cannot contain secrets", "GITHUB PUSH PROTECTION"]
            )
            if is_secret_block:
                reported_paths = extract_github_secret_paths(push.stderr)
                if not reported_paths:
                    reported_paths = sorted({finding.path for finding in scan_for_secrets(folder)})

                if reported_paths and not recovery_attempted:
                    if confirm_secret_history_reset(folder, reported_paths):
                        return setup_git_and_push(
                            folder,
                            authenticated_repo_url,
                            clean_repo_url,
                            commit_message,
                            recovery_attempted=True,
                        )

                render_box(
                    "GITHUB PUSH PROTECTION",
                    [
                        BoxRow("Status", "Push cancelled; GitHub detected a secret.", "danger"),
                        BoxRow(
                            "Required Action",
                            "Remove or rotate the reported secret from every commit, then retry.",
                            "warning",
                        ),
                    ],
                    border_style="#FF5577",
                    title_style="danger",
                )
            elif any(x in push.stderr for x in ["Large files detected", "exceeds GitHub's file size limit", "pre-receive hook declined"]):
                console.print(f"\n[bold red]❌ Push failed due to large files in history.[/bold red]")
                console.print("[yellow]Note: Deleting files isn't enough; they remain in Git's history.[/yellow]")
                
                questions = [
                    inquirer.List('choice',
                                  message="How to proceed?",
                                  choices=['🔄 Attempt Fresh Push (Clean history)', '❌ Cancel'],
                                  default='🔄 Attempt Fresh Push (Clean history)')
                ]
                ans = inquirer.prompt(questions)
                if ans and "Fresh Push" in ans['choice']:
                    console.print("[blue]🧹 Cleaning up history...[/blue]")
                    git_dir = os.path.join(folder, ".git")
                    if os.path.exists(git_dir):
                        shutil.rmtree(git_dir)
                    return setup_git_and_push(
                        folder,
                        authenticated_repo_url,
                        clean_repo_url,
                        commit_message,
                        recovery_attempted=True,
                    )
            else:
                print_error(f"Push failed: {push.stderr}")
            
            return False

def delete_repository(g):
    """Fetch and delete a repository with confirmation"""
    try:
        user = g.get_user()
        with console.status("[blue]Fetching repositories...[/blue]"):
            repos = [repo.name for repo in user.get_repos()]
        
        if not repos:
            console.print("[yellow]⚠️ No repositories found.[/yellow]")
            return

        repo_name = select_repository(
            repos,
            message="Select repository to DELETE",
        )
        if repo_name is None:
            return
        
        confirm_q = [
            inquirer.List('confirm',
                          message=f"Are you ABSOLUTELY sure you want to delete '{repo_name}'?",
                          choices=['No', 'Yes'],
                          default='No')
        ]
        conf = inquirer.prompt(confirm_q)
        if conf and conf['confirm'] == 'Yes':
            repo = user.get_repo(repo_name)
            repo.delete()
            print_success(f"Repository '{repo_name}' deleted!")
        else:
            console.print("[blue]Operation cancelled.[/blue]")
            
    except Exception as e:
        print_error(f"Error deleting repository: {str(e)}")

def download_repository(g, username, token):
    """Clone a selected GitHub repository to a local destination."""
    try:
        user = g.get_user()
        with console.status("[blue]Fetching repositories...[/blue]"):
            repos = sorted([repo.name for repo in user.get_repos()])

        if not repos:
            console.print("[yellow]⚠️ No repositories found.[/yellow]")
            return

        repo_name = select_repository(
            repos,
            message="Select repository to download",
        )
        if repo_name is None:
            return
        setup_tab_completion()
        console.print("\n[bold cyan]📥 Enter destination folder (TAB autocomplete, blank for home directory):[/bold cyan]")
        try:
            destination_input = input("> ").strip()
        except EOFError:
            return

        destination_root = os.path.abspath(
            os.path.expanduser(
                os.path.expandvars(destination_input or "~")
            )
        )
        if not os.path.exists(destination_root):
            create_q = [
                inquirer.List(
                    'create',
                    message=f"Destination '{destination_root}' does not exist. Create it?",
                    choices=['Yes', 'No'],
                    default='Yes'
                )
            ]
            create_ans = inquirer.prompt(create_q)
            if not create_ans or create_ans['create'] != 'Yes':
                console.print("[blue]Operation cancelled.[/blue]")
                return
            os.makedirs(destination_root, exist_ok=True)

        if not os.path.isdir(destination_root):
            print_error("Destination must be a directory.")
            return

        target_path = os.path.join(destination_root, repo_name)
        if os.path.exists(target_path):
            if os.path.isdir(target_path) and os.path.exists(os.path.join(target_path, ".git")):
                update_q = [
                    inquirer.List(
                        'update',
                        message=f"'{target_path}' already exists as a Git repository. Pull latest changes?",
                        choices=['Yes', 'No'],
                        default='Yes'
                    )
                ]
                update_ans = inquirer.prompt(update_q)
                if update_ans and update_ans['update'] == 'Yes':
                    with console.status("[blue]Pulling latest changes...[/blue]"):
                        pull = run_command(["git", "pull", "--ff-only"], cwd=target_path)
                    if pull.returncode == 0:
                        print_success(f"Repository updated: {target_path}")
                    else:
                        print_error(f"Pull failed: {pull.stderr}")
                return

            print_error(f"Target path already exists and is not an existing Git repository: {target_path}")
            return

        repo_url = build_authenticated_repo_url(username, repo_name, token)
        clean_repo_url = build_clean_repo_url(username, repo_name)
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task("[cyan]Cloning repository...", total=None)
            clone = run_command(["git", "clone", repo_url, target_path], cwd=destination_root)
            progress.update(task, description="[cyan]Clone complete")

        if os.path.exists(os.path.join(target_path, ".git")):
            run_command(
                ["git", "remote", "set-url", "origin", clean_repo_url],
                cwd=target_path,
            )

        if clone.returncode == 0:
            print_success(f"Download Complete: {target_path}")
        else:
            print_error(f"Download failed: {clone.stderr}")
    except Exception as e:
        print_error(f"Error downloading repository: {str(e)}")

def show_repository_release(repo):
    """Display the repository's latest GitHub release in a safe text panel."""
    try:
        release = repo.get_latest_release()
        release_number = release.tag_name or "Unknown"
        release_title = release.title or "Untitled release"
        release_message = (release.body or "No release message provided.").strip()
        render_box(
            "LATEST REPOSITORY RELEASE",
            [
                BoxRow("Release Number", release_number),
                BoxRow("Title", release_title),
                BoxRow("Message", release_message),
            ],
        )
        return release
    except UnknownObjectException:
        render_box(
            "REPOSITORY RELEASE",
            [Text("No published releases were found for this repository.", style="warning")],
            border_style="#FFD166",
            title_style="warning",
        )
    except Exception as e:
        console.print(f"[yellow]⚠️ Could not load release information: {str(e)}[/yellow]")
    return None

def _release_bullets(value, fallback):
    """Convert editable semicolon/newline text into Markdown bullets."""
    items = [
        item.strip().lstrip("-*+ ")
        for item in re.split(r"[;\n]+", str(value or ""))
        if item.strip().lstrip("-*+ ")
    ]
    return "\n".join(f"- {item}" for item in items) or f"- {fallback}"


def format_release_message(new_functions, bug_fixes, removed_functions):
    """Build a consistent GitHub release message."""
    return (
        "## New Functions\n"
        f"{_release_bullets(new_functions, 'No new functions detected')}\n\n"
        "## Bug Fixes\n"
        f"{_release_bullets(bug_fixes, 'No bug fixes detected')}\n\n"
        "## Removed Functions\n"
        f"{_release_bullets(removed_functions, 'No removed functions detected')}"
    )


def fetch_remote_readme(repo):
    """Fetch the README currently stored on the repository's default branch."""
    try:
        readme = repo.get_readme()
        return readme.decoded_content.decode("utf-8", errors="ignore"), True
    except Exception:
        return "", False

def fetch_repository_version_tags(repo):
    """Fetch existing release and Git tag names without loading unlimited pages."""
    versions = []
    try:
        versions.extend(
            release.tag_name
            for release in repo.get_releases().get_page(0)
            if release.tag_name
        )
    except Exception:
        pass
    try:
        versions.extend(tag.name for tag in repo.get_tags().get_page(0) if tag.name)
    except Exception:
        pass

    unique_versions = []
    seen = set()
    for version in versions:
        normalized = version.casefold()
        if normalized not in seen:
            seen.add(normalized)
            unique_versions.append(version)
    return unique_versions

def suggest_next_version_tag(existing_versions):
    """Increment the newest conventional numeric version, or begin at v1.0.0."""
    version_pattern = re.compile(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")
    for version in existing_versions:
        match = version_pattern.fullmatch(version.strip())
        if not match:
            continue
        major = int(match.group(1))
        minor = match.group(2)
        patch = match.group(3)
        if minor is None:
            return f"v{major + 1}"
        if patch is None:
            return f"v{major}.{int(minor) + 1}"
        return f"v{major}.{int(minor)}.{int(patch) + 1}"
    return "v1.0.0"

def prepare_repository_release(repo, local_folder):
    """Compare READMEs and prepare editable release details before upload."""
    try:
        try:
            repo.get_commits().get_page(0)
        except Exception:
            print_error("The remote repository has no commit to compare yet.")
            return None

        console.print("\n[cyan]📦 Comparing remote and local README files...[/cyan]")
        existing_versions = fetch_repository_version_tags(repo)
        suggested_version = suggest_next_version_tag(existing_versions)
        old_readme, old_found = fetch_remote_readme(repo)
        new_readme, new_found = read_local_readme(local_folder)
        comparison = compare_readmes(
            old_readme,
            new_readme,
            old_found=old_found,
            new_found=new_found,
        )
        render_box(
            "REPOSITORY VERSION HISTORY",
            [
                BoxRow(
                    "Existing Versions",
                    ", ".join(existing_versions[:20]) or "No releases or tags found",
                ),
                BoxRow("Suggested Next", suggested_version, "success"),
            ],
        )
        render_box(
            "README CHANGE ANALYSIS",
            [
                BoxRow("Remote README", "Found" if old_found else "Not found"),
                BoxRow("Local README", "Found" if new_found else "Not found"),
                BoxRow("Added Lines", str(comparison.added_lines), "success"),
                BoxRow("Removed Lines", str(comparison.removed_lines), "warning"),
                BoxRow(
                    "New Functions",
                    suggestion_text(comparison.new_functions, "None detected"),
                ),
                BoxRow(
                    "Bug Fixes",
                    suggestion_text(comparison.bug_fixes, "None detected"),
                ),
                BoxRow(
                    "Removed Functions",
                    suggestion_text(comparison.removed_functions, "None detected"),
                ),
            ],
        )

        existing_normalized = {version.casefold() for version in existing_versions}
        while True:
            version = Prompt.ask("Enter new release version tag", default=suggested_version).strip()
            if not version:
                print_error("Version tag cannot be empty.")
                continue
            if version.casefold() in existing_normalized:
                print_error(f"Version '{version}' already exists. Choose a new version.")
                continue
            break

        release_title = Prompt.ask(
            "Release title",
            default=comparison.suggested_title(version),
        ).strip()
        new_functions = Prompt.ask(
            "New functions (semicolon separates items)",
            default=suggestion_text(comparison.new_functions, "No new functions detected"),
        ).strip()
        bug_fixes = Prompt.ask(
            "Bug fixes (semicolon separates items)",
            default=suggestion_text(comparison.bug_fixes, "No bug fixes detected"),
        ).strip()
        removed_functions = Prompt.ask(
            "Removed functions (semicolon separates items)",
            default=suggestion_text(
                comparison.removed_functions,
                "No removed functions detected",
            ),
        ).strip()
        release_message = format_release_message(
            new_functions,
            bug_fixes,
            removed_functions,
        )
        render_box(
            f"{version} RELEASE PREVIEW",
            [
                BoxRow("Title", release_title),
                BoxRow("Message", release_message),
            ],
        )
        return ReleasePlan(version, release_title, release_message)
    except Exception as e:
        console.print(f"[yellow]⚠️ Could not prepare release: {str(e)}[/yellow]")
        return None


def publish_repository_release(repo, release_plan):
    """Publish a prepared release after the new repository content is pushed."""
    try:
        release = repo.create_git_release(
            tag=release_plan.tag,
            name=release_plan.title,
            message=release_plan.message,
            draft=False,
            prerelease=False
        )
        print_success(f"Release '{release_plan.tag}' created from the uploaded code!")
        render_box(
            f"{release_plan.tag} RELEASE MESSAGE",
            [
                BoxRow("Title", release.title or release_plan.title),
                BoxRow("Message", release_plan.message),
            ],
            border_style="#39FF88",
            title_style="success",
        )
        return True
    except Exception as e:
        console.print(f"[yellow]⚠️ Upload succeeded, but release creation failed: {str(e)}[/yellow]")
        return False

def show_system_info(username):
    render_box(
        "ACTIVE SESSION",
        [
            BoxRow("CLI Version", f"v{__version__} (Latest)", "brand"),
            BoxRow("Account", username),
            BoxRow("Runtime", f"Python {sys.version.split()[0]} • {sys.platform}"),
            BoxRow("Workspace", os.path.abspath(os.getcwd())),
        ],
    )

def refresh_upstream_status():
    with console.status("[brand]Fetching upstream release and update status...[/brand]"):
        return fetch_upstream_status()

def show_update_status(update_status):
    rows = [
        BoxRow("Local Revision", update_status.local_revision),
        BoxRow("Status", update_status.status_text),
    ]
    if update_status.upstream_revision:
        rows.append(BoxRow("Upstream Revision", update_status.upstream_revision[:12]))
    if update_status.release:
        rows.extend([
            BoxRow("Latest Release", update_status.release.tag),
            BoxRow("Release Title", update_status.release.title),
            BoxRow("Release Notes", update_status.release.notes),
            BoxRow("Release URL", update_status.release.url or UPSTREAM_WEB_URL),
        ])
    elif update_status.error:
        rows.append(BoxRow("Error", update_status.error, "danger"))
    render_box("APPLICATION UPDATE", rows)

def offer_upstream_update(update_status):
    """Offer a safe fast-forward update when the upstream graph is newer."""
    if update_status.state != UpdateState.UPDATE_AVAILABLE:
        return False

    update_q = [
        inquirer.List(
            "update",
            message="Install this update now?",
            choices=["No", "Yes"],
            default="No",
        )
    ]
    answer = inquirer.prompt(update_q)
    if not answer or answer["update"] != "Yes":
        return False

    with console.status("[brand]Fetching and applying fast-forward update...[/brand]"):
        result = apply_upstream_update()
    if result.success:
        print_success(result.message)
    else:
        print_error(result.message)
    return result.changed

# ==================== MAIN FLOW ====================

def main():
    check_git_installed()
    config = load_config()
    ensure_accounts_config(config)
    
    username, token = get_active_account(config)
    if not token or not username:
        print_header()
        username, token = prompt_for_github_account("Initial Authentication Setup")
        save_account(config, username, token)
    
    g = Github(auth=Auth.Token(token))
    update_status = None

    while True:
        print_header()
        show_system_info(username)
        render_box(
            "MAIN MENU",
            [
                Text(
                    "↑/↓ navigate • Enter select • Q quit • R refresh",
                    style="muted",
                    justify="center",
                )
            ],
        )

        choice = select_main_action()

        if choice == MenuAction.EXIT:
            sys.exit(0)
        elif choice == MenuAction.REFRESH:
            update_status = refresh_upstream_status()
            continue
        elif choice == MenuAction.CHECK_UPDATES:
            update_status = refresh_upstream_status()
            show_update_status(update_status)
            if offer_upstream_update(update_status):
                sys.exit(0)
            input("\nPress Enter to return...")
            continue
        elif choice == MenuAction.RESET_AUTH:
            if os.path.exists(CONFIG_FILE): os.remove(CONFIG_FILE)
            print_success("Configuration reset! Restart the script.")
            sys.exit(0)
        elif choice == MenuAction.SWITCH_ACCOUNT:
            username, token = switch_github_account(config)
            g = Github(auth=Auth.Token(token))
            input("\nPress Enter to return...")
            continue
        elif choice == MenuAction.DOWNLOAD:
            download_repository(g, username, token)
            input("\nPress Enter to return...")
            continue
        elif choice == MenuAction.DELETE:
            delete_repository(g)
            input("\nPress Enter to return...")
            continue
            
        # --- UPLOAD FLOW ---
        while True:
            try:
                user = g.get_user()
                current_dir = os.path.abspath(os.environ.get("GITHUB_UPLOADER_CWD") or os.getcwd())
                folder_name = os.path.basename(current_dir)

                # Step 1: Prompt to upload current folder or select another
                folder_confirm_q = [
                    inquirer.List(
                        'folder_choice',
                        message=f"Upload this folder: '{folder_name}' ({current_dir})?",
                        choices=[
                            (f"📁 Upload this folder: '{folder_name}'", "current"),
                            ("🔍 Choose another folder / Enter path", "choose"),
                            ("🔙 Back to Main Menu", "back"),
                        ],
                        carousel=True,
                    )
                ]
                folder_ans = inquirer.prompt(folder_confirm_q)
                if not folder_ans or folder_ans['folder_choice'] == 'back':
                    break

                target_folder = None
                if folder_ans['folder_choice'] == 'current':
                    target_folder = current_dir
                else:
                    setup_tab_completion()
                    console.print("\n[bold cyan]📁 Enter folder path (Press TAB to autocomplete) or folder name to search:[/bold cyan]")
                    try:
                        folder_input = input("> ").strip()
                    except EOFError:
                        break
                    if not folder_input:
                        continue
                    if os.path.exists(folder_input) and os.path.isdir(folder_input):
                        target_folder = os.path.abspath(folder_input)
                    else:
                        target_folder = auto_detect_folder(folder_input)

                if not target_folder or not os.path.isdir(target_folder):
                    print_error("Invalid directory.")
                    input("\nPress Enter to continue...")
                    continue

                # Step 2: Select repository to upload to
                suggested_repo_name = os.path.basename(target_folder)
                with console.status("[blue]Fetching your repositories...[/blue]"):
                    existing_repos = [repo.name for repo in user.get_repos()]

                pinned_repos = [get_folder_remote_repo(target_folder, username)]
                pinned_repos += get_recent_repos(config, username)
                repo_selection = select_repository(
                    existing_repos,
                    message=f"Select repository for '{suggested_repo_name}'",
                    allow_create=True,
                    pinned=pinned_repos,
                )
                if repo_selection is None:
                    # Backed out of repo selection, loops back to folder prompt
                    continue

                repo_name = repo_selection
                if repo_selection == RepositoryAction.CREATE:
                    repo_name = Prompt.ask("\n📦 Enter new repository name", default=suggested_repo_name)
                    if not repo_name.strip():
                        repo_name = suggested_repo_name

                # Step 3: Visibility and commit message
                vis_q = [inquirer.List('visibility', message="Visibility", choices=['Public', 'Private'], default='Public')]
                vis_ans = inquirer.prompt(vis_q)
                if not vis_ans:
                    continue
                is_public = vis_ans['visibility'] == "Public"
                commit_msg = Prompt.ask("Enter commit message", default="Upload via Auto Uploader")

                # Step 4: Execute upload
                try:
                    repo_exists = False
                    release_plan = None
                    try:
                        repo = user.get_repo(repo_name)
                        repo_exists = True
                        print_success(f"Targeting: {repo_name}")
                        show_repository_release(repo)
                        
                        release_q = [inquirer.List(
                            'release',
                            message="Create a release from README changes after upload?",
                            choices=['Yes', 'No'],
                            default='No',
                        )]
                        release_ans = inquirer.prompt(release_q)
                        if release_ans and release_ans['release'] == 'Yes':
                            release_plan = prepare_repository_release(repo, target_folder)
                    except UnknownObjectException:
                        print_step(f"Creating new repository: {repo_name}...")
                        repo = user.create_repo(repo_name, private=not is_public)

                    repo_url = build_authenticated_repo_url(username, repo_name, token)
                    clean_repo_url = build_clean_repo_url(username, repo_name)
                    if setup_git_and_push(
                        target_folder,
                        repo_url,
                        clean_repo_url,
                        commit_msg,
                    ):
                        remember_recent_repo(config, username, repo_name)
                        if release_plan:
                            publish_repository_release(repo, release_plan)
                        print_success(f"Upload Complete: https://github.com/{username}/{repo_name}")
                        input("\nPress Enter to return...")
                        return main() # Full reset / return to main menu
                except Exception as e:
                    print_error(f"Error: {str(e)}")
                    input("\nPress Enter to return...")
                break # Break upload loop
            except Exception as e:
                print_error(f"Session error: {str(e)}")
                input("\nPress Enter to return...")
                break

if __name__ == "__main__":
    main()
