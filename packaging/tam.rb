# Template for Formula/tam.rb in kschulst/homebrew-tap. scripts/release.py fills in
# @VERSION@ and @SHA256@ and pushes the result to the tap.

class Tam < Formula
  desc "Run coding agents in tmux and get every session back after a reboot"
  homepage "https://github.com/kschulst/tam"
  url "https://github.com/kschulst/tam/archive/refs/tags/v@VERSION@.tar.gz"
  sha256 "@SHA256@"
  license "MIT"
  head "https://github.com/kschulst/tam.git", branch: "main"

  depends_on "fzf"
  depends_on "python@3.13"
  depends_on "tmux"

  def install
    # Standard library only: no virtualenv or pip install needed.
    libexec.install "src/tam"
    (bin/"tam").write <<~PYTHON
      #!#{formula_opt_bin("python@3.13")}/python3.13
      import sys
      sys.path.insert(0, "#{opt_libexec}")
      from tam.cli import main
      sys.exit(main())
    PYTHON
  end

  def caveats
    <<~EOS
      Run once to add tam to ~/.tmux.conf, install its Claude Code hooks
      and choose a default agent:
        tam setup
    EOS
  end

  test do
    assert_match "sidebar", shell_output("#{bin}/tam --help")
    ENV["TMUX"] = ""
    assert_match version.to_s, shell_output("#{bin}/tam help --print")
  end
end
