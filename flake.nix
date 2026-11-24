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
          removeX86OnlyBinaries =
            lib.optionalString (system != "x86_64-darwin")
              ''
                find "$app/Contents/Frameworks" -type f | while read -r m; do
                  if file "$m" | grep -q Mach-O; then
                    archs="$(lipo -archs "$m" 2>/dev/null || true)"
                    if [[ $archs = x86_64 ]]; then
                      rm "$m"
                    fi
                  fi
                done
              '';
        in
        {
          alt-tab = pkgs.stdenv.mkDerivation {
            pname = "AltTab";
            inherit version;
            src = alt-tab-src;
            nativeBuildInputs = with pkgs; [
              xcode
              python3
            ];
            # BSD stat lacks -c, which nixpkgs' fixupPhase needs.
            dontFixup = true;

            buildPhase = ''
              export DEVELOPER_DIR='${xcode}/Contents/Developer'
              unset SDKROOT

              # nix builds run with HOME=/var/empty; Xcode needs
              # writable dirs for SwiftPM caches and CoreSimulator.
              # NSHomeDirectory() ignores $HOME, so CFFIXED_USER_HOME
              # must be set instead
              export HOME="$TMPDIR/.home"
              mkdir -p "$HOME/Library/Caches"
              export CFFIXED_USER_HOME="$HOME"
              derived_data="$NIX_BUILD_TOP/DerivedData"

              export PATH="''${PATH:+$PATH:}/usr/bin"
              # Xcode sandboxes SPM package resolution and plugin
              # execution; macOS denies the sandbox inside nix build
              # environments 26.3's raw ld64 lacks driver-style flags
              # (-Xlinker, etc.); linking through the clang driver fixes
              # SPM package targets.
              envs=(
                PATH="$PATH"
                HOME="$HOME"
                CFFIXED_USER_HOME="$CFFIXED_USER_HOME"
                TMPDIR="$TMP_DIR"
                DEVELOPER_DIR="$DEVELOPER_DIR"
                LANG=C
                LC_ALL=C
                TZ=UTC
              )
              xcodebuild_args=(
                $DEVELOPER_DIR/usr/bin/xcodebuild
                -project alt-tab-macos.xcodeproj
                -scheme Debug
                -configuration Debug
                -derivedDataPath "$derived_data"
                CODE_SIGN_IDENTITY=-
                CURRENT_PROJECT_VERSION='${version}'
                APPCENTER_SECRET=
                DEBUG_INFORMATION_FORMAT=none
                OTHER_CODE_SIGN_FLAGS=--timestamp=none
                # ld64 with LC_DYLD_CHAINED_FIXUPS (deployment target >= 12.0)
                # emits duplicate bind slots in __DATA_CONST,__const and picks
                # which duplicate the objc/msgSend stubs load from
                # nondeterministically, so the same sources produce different
                # bytes. Classic dyld info has no such freedom; keep it.
                OTHER_LDFLAGS='$(inherited) -Wl,-no_fixup_chains'
                OTHER_SWIFT_FLAGS='$(inherited) -disable-sandbox'
                ALWAYS_EMBED_SWIFT_STANDARD_LIBRARIES=NO
                LD=$DEVELOPER_DIR/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang
                -IDEPackageSupportDisableManifestSandbox=1
                -IDEPackageSupportDisablePluginExecutionSandbox=1
              )
              (
                cd "$src"
                env -i "''${envs[@]}" "''${xcodebuild_args[@]}"
              )
            '';

            installPhase = ''
              mkdir -p "$out/Applications"
              app="$out/Applications/AltTab.app"
              cp -R "$derived_data/Build/Products/Debug/AltTab.app" "$app"
              chmod -R u+w "$app"

              ${removeX86OnlyBinaries}

              find "$app" -type f | while read -r m; do
                if file "$m" | grep -q Mach-O; then
                  strip -x "$m"
                fi
              done

              find "$app" -type f | while read -r m; do
                if file "$m" | grep -q Mach-O; then
                  for rp in $(otool -l "$m" | sed -n 's/^[[:space:]]*path \(.*\/DerivedData\/Build\/Products\/Debug\/PackageFrameworks\).*/\1/p'); do
                    install_name_tool -delete_rpath "$rp" "$m"
                  done
                fi
              done

              find "$app" -type f | while read -r m; do
                codesign --remove-signature "$m" >/dev/null 2>&1 || true
              done
              find "$app" -depth -name _CodeSignature -type d -exec rm -rf {} +
              codesign --remove-signature "$app" >/dev/null 2>&1 || true

              find "$app" -type f | while read -r m; do
                if file "$m" | grep -q "Mach-O 64-bit"; then
                  python3 '${./canonicalize-macho.py}' "$m"
                fi
              done

              # Sparkle's compiled permission-prompt UI uses two
              # versioned NIB archives. ibtool can reorder their keyed
              # archive objects between builds, so install tracked
              # byte-stable copies.
              nib_dir="$app/Contents/Frameworks/Sparkle.framework/Versions/A/Resources/SUUpdatePermissionPrompt.nib"
              cp '${./canonical-nibs/keyedobjects-101300.nib}' "$nib_dir/keyedobjects-101300.nib"
              cp '${./canonical-nibs/keyedobjects-110000.nib}' "$nib_dir/keyedobjects-110000.nib"

              codesign --force --deep --sign - --timestamp=none "$app"
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
