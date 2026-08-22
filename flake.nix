{
  description = "AltTab, packaged from the latest alt-tab-macos source";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    alt-tab-src = {
      url = "github:lwouis/alt-tab-macos";
      flake = false;
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      alt-tab-src,
    }:
    let
      inherit (nixpkgs) lib;
      systems = [
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      forAllSystems = lib.genAttrs systems;
      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          config.allowUnfreePredicate =
            pkg: builtins.elem (lib.getName pkg) [ "xcode" ];
        };
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          xcode =
            if system == "aarch64-darwin" then
              pkgs.darwin.xcode_26_3_Apple_silicon
            else
              pkgs.darwin.xcode_26_3;
          version = "unstable-" + alt-tab-src.shortRev;
        in
        {
          alt-tab = pkgs.stdenv.mkDerivation {
            pname = "AltTab";
            inherit version;
            src = alt-tab-src;
            nativeBuildInputs = [ xcode ];
            # BSD stat lacks -c, which nixpkgs' fixupPhase needs.
            dontFixup = true;

            buildPhase = ''
              # Overrides the repo's per-dev config (debug.xcconfig
              # includes local.xcconfig when present). Ad-hoc signing
              # avoids the "Local Self-Signed" login-keychain
              # certificate and Darwin TeamID.
              mkdir -p config
              cat >config/local.xcconfig <<XCCONFIG
              CODE_SIGN_IDENTITY = -
              CURRENT_PROJECT_VERSION = ${version}
              APPCENTER_SECRET =
              OTHER_SWIFT_FLAGS = $(inherited) -disable-sandbox
              // Debug DWARF embeds the build dir path, which changes
              // each run (nix uses
              // /nix/var/nix/builds/nix-<pid>-<rand>), making the
              // output non-deterministic.
              DEBUG_INFORMATION_FORMAT = none
              XCCONFIG

              export DEVELOPER_DIR='${xcode}/Contents/Developer'
              unset SDKROOT

              # nix builds run with HOME=/var/empty; Xcode needs
              # writable dirs for SwiftPM caches and CoreSimulator.
              # NSHomeDirectory() ignores $HOME, so CFFIXED_USER_HOME
              # must be set instead
              export HOME="$TMPDIR/.home"
              export CFFIXED_USER_HOME="$HOME"
              mkdir -p "$HOME/Library/Caches"

              export PATH="''${PATH:+$PATH:}/usr/bin"
              # Xcode sandboxes SPM package resolution and plugin
              # execution; macOS denies the sandbox inside nix build
              # environments 26.3's raw ld64 lacks driver-style flags
              # (-Xlinker, etc.); linking through the clang driver fixes
              # SPM package targets.
              xcodebuild_args=(
                $DEVELOPER_DIR/usr/bin/xcodebuild
                -project alt-tab-macos.xcodeproj
                -scheme Debug
                -configuration Debug
                -derivedDataPath DerivedData
                LD="$DEVELOPER_DIR/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang"
                -IDEPackageSupportDisableManifestSandbox=1
                -IDEPackageSupportDisablePluginExecutionSandbox=1
              )
              "''${xcodebuild_args[@]}"
            '';

            installPhase = ''
              mkdir -p $out/Applications
              cp -R DerivedData/Build/Products/Debug/AltTab.app "$out/Applications/"
            '';

            meta = with pkgs.lib; {
              description = "MacOS open-source alt-tab: brings the Windows alt-tab window switcher to macOS";
              homepage = "https://alt-tab.app/";
              license = licenses.gpl3;
              maintainers = [ ];
              platforms = platforms.darwin;
            };
          };
          default = self.packages.${system}.alt-tab;
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program =
            let
              defaultPkg = self.packages.${system}.default;
            in
            "${defaultPkg}/Applications/AltTab.app/Contents/MacOS/AltTab";
        };
      });
    };
}
