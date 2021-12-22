# Forge add-on
The forge add-on allows you to install and run Minecraft with forge mod loader in a single command 
line!

### Usage
This add-on extends the syntax accepted by the [start](/README.md#start-the-game) sub-command, by 
prepending the version with `forge:`. Almost all releases are supported by forge, the latest 
releases are often supported, if not please refer to forge website. You can also append either
`-recommended` or `-latest` to the version to take the corresponding version according to the
forge public information, this is reflecting the "Download Latest" and "Download Recommended" on
the forge website. You can also use version aliases like `release` or equivalent empty version 
(just `forge:`). You can also give the exact forge version like `1.18.1-39.0.7`, in such cases,
no HTTP request is made if the version is already installed.

### Examples
```sh
portablemc start forge:               # Install recommended forge version for latest release
portablemc start forge:release        # Same as above
portablemc start forge:1.18.1         # Install recommended forge for 1.18.1
portablemc start forge:1.18.1-39.0.7  # Install the exact forge version 1.18.1-39.0.7
```

### Credits
- [Forge Website](https://files.minecraftforge.net/net/minecraftforge/forge/)
- Consider supporting [LexManos](https://www.patreon.com/LexManos/)