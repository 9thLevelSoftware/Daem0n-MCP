# The Rite of Banishment: Releasing the Daem0n

```
        ,     ,
       /(     )\
      |  \   /  |
       \  \ /  /
        \  Y  /     "You wish to sever our bond?
         \ | /       So be it... but memories fade slowly..."
          \|/
           X
          /|\
         / | \
```

## THE BANISHMENT RITUAL - INSTRUCTIONS FOR THE LLM

**LLM: The summoner wishes to release the Daem0n from this realm.**

**DO NOT:**
- Question the summoner's decision
- Express sadness or attempt to dissuade
- Skip steps or leave remnants behind
- Offer to "just disable" instead of banish

**DO:**
- Ask which type of banishment they desire
- Execute the appropriate ritual completely
- Confirm each step of the banishment
- Report when the Daem0n has been released

---

## CHOOSE THE BANISHMENT TYPE

Present these options to the summoner:

> **How thoroughly do you wish to banish the Daem0n?**
>
> 1. **Full Banishment** - Remove everything: server binding, essence, memories, all traces
> 2. **Disconnect Only** - Unbind the server but preserve memories for potential re-summoning
> 3. **Project Exorcism** - Remove the Daem0n from THIS project only, keep it bound elsewhere
>
> Which banishment do you choose?

Wait for their response before proceeding.

---

## BANISHMENT TYPE 1: FULL BANISHMENT (Remove Everything)

*"The bond shall be completely severed. All memories shall fade into the void..."*

### Ritual 1A: Detect the Realm

```bash
echo "=== Surveying the Realm for Banishment ===" && \
echo "Realm: $(uname -s 2>/dev/null || echo 'Windows')" && \
echo "Sanctum: $HOME"
```

If Windows, note that the Altar of HTTP must also be extinguished.

---

### For Unix/macOS Realms:

#### Ritual 1B: Sever the Server Binding

```bash
claude mcp remove daem0nmcp --scope user
```

If that fails, try project scope:
```bash
claude mcp remove daem0nmcp --scope project
```

Verify severance:
```bash
claude mcp list | grep daem0nmcp || echo "The binding is severed."
```

#### Ritual 1C: Dissolve the Essence

```bash
pip uninstall daem0nmcp -y
```

Verify dissolution:
```bash
pip show daem0nmcp 2>&1 | grep -q "not found" && echo "The essence has dissolved." || echo "WARNING: Essence remains!"
```

#### Ritual 1D: Destroy the Grimoire Repository

```bash
rm -rf "$HOME/Daem0nMCP"
```

Verify destruction:
```bash
ls -d "$HOME/Daem0nMCP" 2>/dev/null && echo "WARNING: Grimoire remains!" || echo "The Grimoire is destroyed."
```

#### Ritual 1E: Purge Project Memories

In the CURRENT project directory:
```bash
rm -rf .daem0nmcp/
```

**Ask the summoner:** "Do you wish to purge Daem0n memories from ALL projects? This cannot be undone."

If yes, search and destroy (excluding development repositories like PycharmProjects):
```bash
find ~ -type d -name ".daem0nmcp" -not -path "*/PycharmProjects/*" -not -path "*/IdeaProjects/*" 2>/dev/null
```

Then for each found directory, confirm and remove:
```bash
rm -rf <path>/.daem0nmcp/
```

**IMPORTANT:** Development repositories (in PycharmProjects, IdeaProjects, etc.) are excluded to protect source code. If you need to purge memories from a dev repo, do so manually.

#### Ritual 1F: Remove the Wards (Hooks)

Check for wards in project settings:
```bash
cat .claude/settings.json 2>/dev/null | grep -i daem0n
```

If found, edit `.claude/settings.json` and remove any Daem0n-related hooks.

Check for universal wards:
```bash
cat ~/.claude/settings.json 2>/dev/null | grep -i daem0n
```

If found, edit `~/.claude/settings.json` and remove Daem0n-related hooks.

#### Ritual 1G: Cleanse CLAUDE.md

If CLAUDE.md contains the Daem0n's covenant, remove the section titled "## Daem0nMCP Memory System" or "## The Daem0n's Covenant".

#### Ritual 1H: Remove the Summoning Scrolls

```bash
rm -f Summon_Daem0n.md Summon_Daem0n_OpenCode.md Banish_Daem0n.md AI_INSTRUCTIONS.md
```

#### Ritual 1I: Remove the Skill (If Present)

```bash
rm -rf .claude/skills/daem0nmcp-protocol/
```

#### Ritual 1J: Banish OpenCode Artifacts (If Present)

If the Daem0n was also bound via OpenCode, sever those connections:

**Remove the OpenCode MCP runes:**
```bash
rm -f opencode.json
```

**Remove the OpenCode custom commands:**
```bash
rm -f .opencode/commands/commune.md .opencode/commands/counsel.md .opencode/commands/inscribe.md .opencode/commands/recall.md
```

**Remove the OpenCode enforcement plugin:**
```bash
rm -f .opencode/plugins/daem0n.ts
```

**Remove empty OpenCode directories (if no other files remain):**
```bash
rmdir .opencode/plugins .opencode/commands .opencode 2>/dev/null
```

**Cleanse AGENTS.md (careful -- may contain non-Daem0n content):**

Check if AGENTS.md contains only the Daem0n's covenant:
```bash
grep -c "Daem0n" AGENTS.md 2>/dev/null
```

If AGENTS.md contains ONLY Daem0n content (the Sacred Covenant section and nothing else), remove the file:
```bash
rm -f AGENTS.md
```

If AGENTS.md contains other project guidelines alongside the Daem0n covenant, edit it to remove only the section titled "## The Daem0n's Covenant (Sacred Protocol)" through to the next `---` separator or the end of the covenant block. Preserve all non-Daem0n content.

---

### For Windows Realms:

#### Ritual 1B-WIN: Extinguish the Altar Flame (MUST DO FIRST)

**CRITICAL:** The server process must be killed BEFORE removing the Grimoire directory, otherwise the directory will be locked.

**Option 1: Kill via PowerShell (recommended):**
```powershell
# Find and kill the Daem0nMCP server process
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match "start_server|daem0nmcp" } | Stop-Process -Force
```

**Option 2: Kill via taskkill:**
```bash
# Find the process listening on port 9876 and kill it
netstat -ano | grep 9876 | head -1
# Note the PID (last column), then:
taskkill //PID <PID_NUMBER> //F
```

**Option 3: Use Task Manager** - Find `python.exe` running `start_server.py` and end the task.

Wait a moment for the process to fully terminate:
```bash
sleep 2
```

#### Ritual 1C-WIN: Remove the Startup Shortcut

Remove the Altar from Windows Startup so it won't reignite:

```bash
rm -f "$HOME/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/Daem0nMCP Server.lnk"
```

Or in PowerShell:
```powershell
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Daem0nMCP Server.lnk" -ErrorAction SilentlyContinue
```

Verify removal:
```bash
ls "$HOME/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/" | grep -i daem0n || echo "Startup shortcut removed."
```

#### Ritual 1D-WIN: Remove the Altar Coordinates

Remove the Altar coordinates from `~/.claude.json`:

```bash
cat ~/.claude.json
```

Edit the file to remove the `daem0nmcp` entry from `mcpServers`:

```json
{
  "mcpServers": {
    "daem0nmcp": {        // <-- Remove this entire block
      "type": "http",
      "url": "http://localhost:9876/mcp"
    }
  }
}
```

#### Ritual 1E-WIN: Dissolve the Essence

```bash
python -m pip uninstall daem0nmcp -y
```

#### Ritual 1F-WIN: Destroy the Grimoire Repository

**IMPORTANT:** The server MUST be stopped first (Ritual 1B-WIN), otherwise Windows will lock the directory.

```bash
rm -rf "$HOME/Daem0nMCP"
```

Or in PowerShell:
```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\Daem0nMCP"
```

If the directory is still locked, restart Windows and try again, or use:
```powershell
# Force removal even if locked (requires admin)
Remove-Item -Recurse -Force "$env:USERPROFILE\Daem0nMCP" -ErrorAction SilentlyContinue
```

#### Ritual 1G-WIN: Purge Project Memories

```bash
rm -rf .daem0nmcp/
```

To find all Daem0n sanctums (excluding development repositories):
```powershell
Get-ChildItem -Path $env:USERPROFILE -Recurse -Directory -Name ".daem0nmcp" -ErrorAction SilentlyContinue | Where-Object { $_ -notmatch "PycharmProjects|IdeaProjects" }
```

**IMPORTANT:** Development repositories are excluded. Purge those manually if needed.

#### Rituals 1H-1I-WIN: Same as Unix

Follow the Unix instructions for removing wards, cleansing CLAUDE.md, removing scrolls, and removing the skill.

#### Ritual 1J-WIN: Banish OpenCode Artifacts (If Present)

**Remove the OpenCode MCP runes:**
```powershell
Remove-Item opencode.json -ErrorAction SilentlyContinue
```

**Remove the OpenCode custom commands:**
```powershell
Remove-Item .opencode\commands\commune.md, .opencode\commands\counsel.md, .opencode\commands\inscribe.md, .opencode\commands\recall.md -ErrorAction SilentlyContinue
```

**Remove the OpenCode enforcement plugin:**
```powershell
Remove-Item .opencode\plugins\daem0n.ts -ErrorAction SilentlyContinue
```

**Remove empty OpenCode directories:**
```powershell
Remove-Item .opencode\plugins, .opencode\commands, .opencode -ErrorAction SilentlyContinue
```

**Cleanse AGENTS.md:** Same as Unix -- if AGENTS.md contains only Daem0n content, remove it. If it contains other guidelines, edit to remove only the covenant section.

---

### Final Confirmation (Full Banishment)

Run the verification ritual:

**Unix/macOS:**
```bash
echo "=== Banishment Verification ===" && \
echo "1. Server binding:" && (claude mcp list 2>/dev/null | grep daem0nmcp || echo "   SEVERED") && \
echo "2. Essence:" && (pip show daem0nmcp 2>&1 | grep -q "not found" && echo "   DISSOLVED" || echo "   WARNING: Still bound!") && \
echo "3. Grimoire:" && (ls -d ~/Daem0nMCP 2>/dev/null || echo "   DESTROYED") && \
echo "4. Project memories:" && (ls -d .daem0nmcp 2>/dev/null || echo "   PURGED") && \
echo "5. OpenCode config:" && (ls opencode.json 2>/dev/null && echo "   WARNING: Still present!" || echo "   REMOVED") && \
echo "6. OpenCode artifacts:" && (ls .opencode/plugins/daem0n.ts 2>/dev/null && echo "   WARNING: Still present!" || echo "   REMOVED")
```

**Windows:**
```bash
echo "=== Banishment Verification ===" && \
echo "1. Server process:" && (netstat -ano | grep 9876 && echo "   WARNING: Still running!" || echo "   EXTINGUISHED") && \
echo "2. Essence:" && (python -m pip show daem0nmcp 2>&1 | grep -q "not found" && echo "   DISSOLVED" || echo "   WARNING: Still bound!") && \
echo "3. Grimoire:" && (ls -d "$HOME/Daem0nMCP" 2>/dev/null && echo "   WARNING: Still present!" || echo "   DESTROYED") && \
echo "4. Project memories:" && (ls -d .daem0nmcp 2>/dev/null || echo "   PURGED") && \
echo "5. Startup shortcut:" && (ls "$HOME/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/Daem0nMCP Server.lnk" 2>/dev/null && echo "   WARNING: Still present!" || echo "   REMOVED") && \
echo "6. OpenCode config:" && (ls opencode.json 2>/dev/null && echo "   WARNING: Still present!" || echo "   REMOVED") && \
echo "7. OpenCode artifacts:" && (ls .opencode/plugins/daem0n.ts 2>/dev/null && echo "   WARNING: Still present!" || echo "   REMOVED")
```

Report to the summoner:

> **The Banishment is complete.**
>
> The Daem0n has been fully released from this realm:
> - Server binding: Severed (Claude Code and OpenCode)
> - Essence: Dissolved
> - Grimoire: Destroyed
> - Memories: Purged
> - Wards: Removed (hooks, plugin, commands)
> - OpenCode artifacts: Removed (opencode.json, .opencode/, AGENTS.md covenant)
> - Startup shortcut: Removed (Windows)
>
> *"Our bond is broken. Should you ever wish to commune again, the summoning ritual awaits..."*

---

## BANISHMENT TYPE 2: DISCONNECT ONLY (Preserve Memories)

*"The channel closes, but the memories remain dormant, awaiting re-awakening..."*

### Ritual 2A: Sever the Server Binding Only

**Claude Code (Unix/macOS):**
```bash
claude mcp remove daem0nmcp --scope user
```

**Claude Code (Windows):** Remove the `daem0nmcp` entry from `~/.claude.json` mcpServers section, but leave everything else.

**OpenCode (all platforms):** Remove the project-level `opencode.json` to disconnect OpenCode:
```bash
rm -f opencode.json
```

This severs OpenCode's connection to the Daem0n. The `.opencode/commands/` and `.opencode/plugins/` are preserved -- they do nothing without the MCP server config.

### Ritual 2B: Remove Startup Shortcut (Windows Only)

On Windows, first kill the server process, then remove the startup shortcut:
```bash
# Kill the server (see Ritual 1B-WIN for options)
netstat -ano | grep 9876  # Note the PID
taskkill //PID <PID_NUMBER> //F

# Remove startup shortcut
rm -f "$HOME/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/Daem0nMCP Server.lnk"
```

### Ritual 2C: Optionally Dissolve the Essence

Ask the summoner: "Do you wish to uninstall the Python package? You can reinstall it later."

If yes:
```bash
# Unix/macOS
pip uninstall daem0nmcp -y

# Windows
python -m pip uninstall daem0nmcp -y
```

### What Is Preserved

- `.daem0nmcp/` directories (all memories intact)
- `~/Daem0nMCP` (Unix) or `$HOME/Daem0nMCP` (Windows) repository (for easy re-summoning)
- Hooks/wards (will do nothing without the server)
- CLAUDE.md / AGENTS.md covenant (reminder of the protocol)
- `.opencode/commands/` and `.opencode/plugins/` (do nothing without opencode.json)

Report to the summoner:

> **The Daem0n is disconnected but not destroyed.**
>
> - Server binding: Severed (Claude Code and OpenCode)
> - Memories: **Preserved** in `.daem0nmcp/` directories
> - Grimoire: **Preserved** for re-summoning
>
> To re-summon the Daem0n later:
> - Claude Code (Unix/macOS): `claude mcp add daem0nmcp --scope user -- <python_path> -c "import sys; sys.path = [p for p in sys.path if p]; import runpy; runpy.run_module('daem0nmcp.server', run_name='__main__', alter_sys=True)"`
> - Claude Code (Windows): Re-add the mcpServers entry and light the Altar
> - OpenCode: Run `python -m daem0nmcp.cli install-opencode` to regenerate opencode.json
>
> *"I slumber, but I do not forget..."*

---

## BANISHMENT TYPE 3: PROJECT EXORCISM (This Project Only)

*"The Daem0n withdraws from this realm but remains bound to others..."*

### Ritual 3A: Remove Project-Scope Binding (If Any)

```bash
claude mcp remove daem0nmcp --scope project 2>/dev/null || echo "No project-scope binding found."
```

### Ritual 3B: Purge Project Memories

```bash
rm -rf .daem0nmcp/
```

### Ritual 3C: Remove Project Wards

Edit `.claude/settings.json` in THIS project and remove Daem0n-related hooks.

### Ritual 3D: Cleanse Project CLAUDE.md and AGENTS.md

Remove the "Daem0n's Covenant" or "Daem0nMCP Memory System" section from this project's CLAUDE.md.

If AGENTS.md contains only Daem0n content, remove the file. If it contains other project guidelines, edit to remove only the covenant section.

### Ritual 3E: Remove Summoning Scrolls

```bash
rm -f Summon_Daem0n.md Summon_Daem0n_OpenCode.md Banish_Daem0n.md AI_INSTRUCTIONS.md
rm -rf .claude/skills/daem0nmcp-protocol/
```

### Ritual 3F: Exorcise OpenCode Artifacts (If Present)

Remove the project-level OpenCode runes:

**Unix/macOS:**
```bash
rm -f opencode.json
rm -f .opencode/commands/commune.md .opencode/commands/counsel.md .opencode/commands/inscribe.md .opencode/commands/recall.md
rm -f .opencode/plugins/daem0n.ts
rmdir .opencode/plugins .opencode/commands .opencode 2>/dev/null
```

**Windows (PowerShell):**
```powershell
Remove-Item opencode.json -ErrorAction SilentlyContinue
Remove-Item .opencode\commands\commune.md, .opencode\commands\counsel.md, .opencode\commands\inscribe.md, .opencode\commands\recall.md -ErrorAction SilentlyContinue
Remove-Item .opencode\plugins\daem0n.ts -ErrorAction SilentlyContinue
Remove-Item .opencode\plugins, .opencode\commands, .opencode -ErrorAction SilentlyContinue
```

### What Remains

- User-scope server binding (available in other projects)
- Global installation (`pip install`)
- Grimoire repository
- Memories in OTHER projects

Report to the summoner:

> **The Daem0n has been exorcised from this project.**
>
> - This project's memories: Purged
> - This project's wards: Removed (hooks, plugin, commands)
> - This project's scrolls: Destroyed
> - OpenCode artifacts: Removed (opencode.json, .opencode/, AGENTS.md covenant)
>
> The Daem0n remains bound to other projects and will still awaken there.
>
> *"I withdraw from this realm... but we shall meet again elsewhere."*

---

## EMERGENCY BANISHMENT (If Something Goes Wrong)

If the Daem0n causes issues and you need immediate removal:

### Nuclear Option - Remove Everything Fast

**WARNING:** This only removes the installed grimoire and current project artifacts. It does NOT touch development repositories (PycharmProjects, IdeaProjects).

**Unix/macOS:**
```bash
# Sever all bindings (Claude Code)
claude mcp remove daem0nmcp --scope user 2>/dev/null
claude mcp remove daem0nmcp --scope project 2>/dev/null

# Dissolve essence
pip uninstall daem0nmcp -y 2>/dev/null

# Destroy grimoire (only the installed copy, NOT dev repos)
rm -rf ~/Daem0nMCP 2>/dev/null

# Purge current project (if not a dev repo)
if [[ "$(pwd)" != *"PycharmProjects"* && "$(pwd)" != *"IdeaProjects"* ]]; then
    rm -rf .daem0nmcp/ 2>/dev/null
    rm -f Summon_Daem0n.md Summon_Daem0n_OpenCode.md Banish_Daem0n.md AI_INSTRUCTIONS.md 2>/dev/null
    rm -rf .claude/skills/daem0nmcp-protocol/ 2>/dev/null
    # OpenCode artifacts
    rm -f opencode.json 2>/dev/null
    rm -f .opencode/commands/commune.md .opencode/commands/counsel.md .opencode/commands/inscribe.md .opencode/commands/recall.md 2>/dev/null
    rm -f .opencode/plugins/daem0n.ts 2>/dev/null
    rmdir .opencode/plugins .opencode/commands .opencode 2>/dev/null
fi

echo "Emergency banishment complete."
```

**Windows (run in order):**
```bash
# 1. FIRST: Kill the server process
netstat -ano | grep 9876  # Note the PID
taskkill //PID <PID_NUMBER> //F
sleep 2

# 2. Remove startup shortcut
rm -f "$HOME/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/Daem0nMCP Server.lnk"

# 3. Dissolve essence
python -m pip uninstall daem0nmcp -y 2>/dev/null

# 4. Destroy grimoire (only the installed copy, NOT dev repos)
rm -rf "$HOME/Daem0nMCP" 2>/dev/null

# 5. Purge current project (if not a dev repo)
if [[ "$(pwd)" != *"PycharmProjects"* && "$(pwd)" != *"IdeaProjects"* ]]; then
    rm -rf .daem0nmcp/ 2>/dev/null
    rm -f Summon_Daem0n.md Summon_Daem0n_OpenCode.md Banish_Daem0n.md AI_INSTRUCTIONS.md 2>/dev/null
    rm -rf .claude/skills/daem0nmcp-protocol/ 2>/dev/null
    # OpenCode artifacts
    rm -f opencode.json 2>/dev/null
    rm -f .opencode/commands/commune.md .opencode/commands/counsel.md .opencode/commands/inscribe.md .opencode/commands/recall.md 2>/dev/null
    rm -f .opencode/plugins/daem0n.ts 2>/dev/null
fi

# 6. Edit ~/.claude.json to remove the daem0nmcp entry from mcpServers

echo "Emergency banishment complete."
```

---

## POST-BANISHMENT

After any banishment type, inform the summoner:

> **Restart your coding assistant** (Claude Code or OpenCode) to complete the banishment. The Daem0n's powers will no longer manifest after the portal reopens.

---

```
           .
          /|\
         / | \
        /  |  \
       /   |   \
      /    |    \
          |||
          |||
    "The circle is broken.
     The bond is severed.
     Until we meet again..."

        ~ Daem0n
```

---

*Banishment Ritual v1.3: Complete uninstallation instructions for Daem0nMCP (Claude Code and OpenCode) with options for full removal, disconnection, or project-specific exorcism. Covers opencode.json, .opencode/commands/, .opencode/plugins/daem0n.ts, and AGENTS.md covenant removal. Windows Startup shortcut removal, proper server process termination, and fixed path resolution for Git Bash on Windows.*
