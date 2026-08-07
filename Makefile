.PHONY: install test demo

install:
	python -m pip install -e '.[dev]'

test:
	pytest -q

demo:
	rm -rf /tmp/sdai-demo
	mkdir -p /tmp/sdai-demo
	sdai init --path /tmp/sdai-demo
	sdai feature DEMO-1 --title "Demo feature" --description "Demonstrate the SD-AI lifecycle" --path /tmp/sdai-demo
	sdai run DEMO-1 --workflow standard --path /tmp/sdai-demo
	sdai validate DEMO-1 --workflow standard --path /tmp/sdai-demo
