import os
import sys
import subprocess
import json
import platform
import webbrowser
import urllib.request
import time
import zipfile
from pathlib import Path
from rich.console import Console
from rich.prompt import Prompt, Confirm
from rich.panel import Panel
from rich.progress import track
from rich import print
from github import Github

console = Console()

CONFIG_FILE = "config.json"
LOG_FILE = "upload.log"

def check_git_installed():
    result = subprocess.run("git --version", shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        console.print("[bold red]❌ Git is not installed. Please install Git first.[/bold red]")
        sys.exit(1)

def check_internet():
    try:
        urllib.request.urlopen("https://github.com", timeout=5)
    except:
        console.print("[red]❌ No internet connection. Please check and try again.[/red]")
        sys.exit(1)

def clear_terminal():
    os.system('cls' if os.name == 'nt' else 'clear')

def log(message):
    with open(LOG_FILE, "a") as f:
        f.write(f"[{time.ctime()}] {message}\n")

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

def run_command(command, cwd=None):
    result = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        console.print(Panel.fit(f"[bold red]Error running: {command}[/bold red]\n{result.stderr.strip()}"))
    return result

def search_folder(input_path):
    input_path = input_path.strip().replace("  ", " ").lstrip("/\\")
    if os.name != 'nt' and "/sdcard" in input_path:
        possible_paths = [
            f"/storage/emulated/0/{input_path.split('/sdcard/')[-1]}",
            f"/sdcard/{input_path.split('/sdcard/')[-1]}"
        ]
        for p in possible_paths:
            if os.path.exists(p):
                return os.path.abspath(p)

    drives = ['C:/', 'D:/', 'E:/'] if os.name == 'nt' else ['/']
    for drive in drives:
        for root, dirs, _ in os.walk(drive):
            if input_path.lower() in root.lower():
                return os.path.abspath(root)
    return None

def create_repo(g, username, repo_name, private):
    user = g.get_user()
    try:
        repo = user.get_repo(repo_name)
        console.print(f"[yellow]✓ Repository '{repo_name}' already exists.[/yellow]")
        return repo
    except:
        repo = user.create_repo(name=repo_name, private=private)
        console.print(f"[green]✓ Repository '{repo_name}' created.[/green]")
        return repo

def setup_git(folder, repo_url, commit_message):
    run_command(f"git config --global --add safe.directory '{folder}'")
    steps = [
        "git init",
        "git remote remove origin",
        f"git remote add origin {repo_url}",
        "git add .",
        'git config user.email "auto@upload.bot"',
        'git config user.name "GitUploaderBot"',
        f'git commit -m "{commit_message}"',
        "git branch -M main",
        "git pull origin main --allow-unrelated-histories",
        "git push -u origin main"
    ]
    for step in track(steps, description="[cyan]Uploading files to GitHub..."):
        run_command(step, cwd=folder)
        log(f"Executed: {step}")

def extract_if_zip(path):
    if path.endswith(".zip") and os.path.exists(path):
        if Confirm.ask("Detected a zip file. Do you want to extract it?", default=True):
            extract_folder = os.path.splitext(path)[0]
            with zipfile.ZipFile(path, 'r') as zip_ref:
                zip_ref.extractall(extract_folder)
            console.print(f"[green]✓ Extracted to: {extract_folder}[/green]")
            return extract_folder
    return path

def prompt_update_config(config):
    updated = False
    for key in ["token", "username", "repo_name", "folder_path", "is_public", "commit_msg"]:
        current_value = config.get(key)
        if Confirm.ask(f"Do you want to update {key}?", default=False):
            if key == "token":
                new_value = Prompt.ask("Enter new GitHub token", password=True)
            elif key == "is_public":
                new_value = Confirm.ask("Upload as public repo? (y = public, n = private)", default=True)
            else:
                new_value = Prompt.ask(f"Enter new value for {key}")
            config[key] = new_value
            updated = True
    return updated

def main():
    clear_terminal()
    console.print(Panel.fit("🛰️  [bold cyan]GitHub Auto Uploader[/bold cyan]\n[dim]powered by Rich[/dim]"))

    check_git_installed()
    check_internet()

    config = load_config()
    if config:
        if Confirm.ask("Do you want to update existing config?", default=True):
            if prompt_update_config(config):
                save_config(config)
                console.print("[green]✓ Config updated.[/green]")
        else:
            if Confirm.ask("Do you want to upload in another repo?", default=True):
                config["repo_name"] = Prompt.ask("Enter new repository name")
                config["username"] = Prompt.ask("Enter GitHub username")
                save_config(config)
                console.print("[green]✓ Repository info updated.[/green]")
            else:
                if os.path.exists(config.get("folder_path", "")):
                    existing_files = os.listdir(config["folder_path"])
                    if existing_files:
                        console.print(f"[yellow]✓ Files already exist in repo: {existing_files}[/yellow]")
                        action = Prompt.ask("Do you want to [r]eplace or upload to [an]other repo?", choices=["r", "an"])
                        if action == "an":
                            config["repo_name"] = Prompt.ask("Enter new repository name")
                            config["username"] = Prompt.ask("Enter GitHub username")
                            save_config(config)
    else:
        token = Prompt.ask("Enter GitHub token (or 'h' for help)", password=True)
        if token.lower() == 'h':
            webbrowser.open("https://github.com/settings/tokens")
            token = Prompt.ask("Paste your GitHub token", password=True)

        username = Prompt.ask("Enter GitHub username")
        repo_name = Prompt.ask("Enter repository name")
        folder = Prompt.ask("Enter folder path to upload (or .zip file)")
        folder_path = search_folder(folder)
        while not folder_path or not os.path.exists(folder_path):
            console.print(f"[red]❌ Path '{folder}' not found on your system![/red]")
            folder = Prompt.ask("Enter folder path to upload")
            folder_path = search_folder(folder)

        folder_path = extract_if_zip(folder_path)

        is_public = Confirm.ask("Upload as public repo? (y = public, n = private)", default=True)
        commit_msg = Prompt.ask("Enter commit message", default="Upload by bot")

        config = {
            "token": token,
            "username": username,
            "repo_name": repo_name,
            "folder_path": folder_path,
            "is_public": is_public,
            "commit_msg": commit_msg
        }
        save_config(config)
        console.print("[green]✓ Config saved to config.json[/green]")

    token = config["token"]
    username = config["username"]
    repo_name = config["repo_name"]
    folder_path = config["folder_path"]
    is_public = config["is_public"]
    commit_msg = config["commit_msg"]

    folder_path = extract_if_zip(folder_path)
    g = Github(token)
    repo = create_repo(g, username, repo_name, not is_public)
    repo_url = f"https://{token}@github.com/{username}/{repo_name}.git"
    setup_git(folder_path, repo_url, commit_msg)

    console.print(Panel.fit("✓ Files uploaded to GitHub successfully!", style="bold green"))
    log("Upload complete.")

if __name__ == "__main__":
    main()
