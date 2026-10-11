# Diskclosure for Windows

A Windows version of Diskclosure. It lists common folders, the largest files, and copies that share a size. Enter opens the selection in File Explorer. Anything you remove goes to the Recycle Bin, and only after you confirm.

The program walks folders itself. It does not follow junctions or other disks, so a link such as `Application Data` is not counted twice.

## Executable

On a Windows PC, from this `windows` folder:

```powershell
.\build.ps1
.\dist\Diskclosure.exe
```

That produces a single console program, `dist\Diskclosure.exe`. Python does not need to be installed on the machine that runs the finished program.

Pushing changes under `windows/` also builds `Diskclosure.exe` with GitHub Actions. Download it from the workflow's **Diskclosure-windows** artifact.

## Run from source

Windows, Python 3.11 or newer:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e .
.\.venv\Scripts\windiskclosure
```

## How to use it

The first screen is **Common folders**. Sizes fill in while your user profile is measured. That walk can take several minutes on a full disk. AppData is inside your profile, so those sizes overlap.

| Key | Action |
| --- | --- |
| Enter | Open the folder in File Explorer. A file is shown in its folder. |
| l | List what is inside the selected folder |
| Backspace | Go back |
| s | Largest files in the selected folder |
| c | On **Same size**, checksum the copies |
| t | Move the selection to the Recycle Bin, after you confirm |
| / | Filter the list |
| r | Measure again |
| q | Quit |

**Largest files** keeps the biggest files it has seen. **Same size** groups files of at least 10 MB that have the same length. Same length is not proof the contents match. Press `c` before deleting one.

`t` can move an item in your user profile, or a normal file on another disk. It will not remove your profile itself, standard folders such as Desktop, Documents, and Downloads, or anything under Windows, Program Files, or ProgramData. Windows puts the item in the Recycle Bin, so you can restore it.

Some folders stay empty or show **no access** until the program is allowed to read them.

## Tests

```powershell
python -m pip install -e .
python -m unittest discover -s tests -t .
```
