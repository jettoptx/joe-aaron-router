# OPTX GENSYS Xahau Hook Build
# Requires: wasmcc (Xahau Hook Compiler)
# See: https://hooks.xahau.network/docs/getting-started

HOOK_ACCOUNT = rLXCpNStZodh9HjXn5DyoSFMKies1vKBUG

.PHONY: build deploy clean test

build:
	wasmcc hook.c -o hook.wasm

deploy: build
	hdc $(HOOK_ACCOUNT) -i hook.wasm -a 4096 -r 64 -f

test:
	@echo "Test payment with gaze hash memo:"
	@echo "  Account: $(HOOK_ACCOUNT)"
	@echo "  Amount: 1 XRP"
	@echo "  Memo: <32-byte SHA256 of AARON gaze attestation>"

clean:
	rm -f hook.wasm
