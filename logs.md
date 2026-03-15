2026-03-15T15:26:30.470972764Z 10.20.135.162 - - [15/Mar/2026:15:26:30 +0000] "GET /robots.txt HTTP/1.1" 404 207 "-" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_1) AppleWebKit/600.2.5 (KHTML, like Gecko) Version/8.0.2 Safari/600.2.5 (Gort)"
2026-03-15T15:49:40.780332777Z 10.18.137.69 - - [15/Mar/2026:15:49:40 +0000] "GET / HTTP/1.1" 302 217 "http://easyflex.onrender.com" "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 Mobile/15E148 Safari/604.1"
2026-03-15T15:49:41.629622898Z 10.23.219.133 - - [15/Mar/2026:15:49:41 +0000] "GET /login?next=%2F HTTP/1.1" 200 2424 "https://easyflex.onrender.com/" "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 Mobile/15E148 Safari/604.1"
2026-03-15T18:46:07.808674028Z ==> Deploying...
2026-03-15T18:46:56.415998347Z ==> Running 'gunicorn webapp.app:app --timeout 300 --graceful-timeout 30 --workers 2'
2026-03-15T18:47:04.214873899Z [2026-03-15 18:47:04 +0000] [56] [INFO] Starting gunicorn 25.1.0
2026-03-15T18:47:04.215357563Z [2026-03-15 18:47:04 +0000] [56] [INFO] Listening at: http://0.0.0.0:10000 (56)
2026-03-15T18:47:04.215416545Z [2026-03-15 18:47:04 +0000] [56] [INFO] Using worker: sync
2026-03-15T18:47:04.233345798Z [2026-03-15 18:47:04 +0000] [56] [INFO] Control socket listening at /opt/render/project/src/gunicorn.ctl
2026-03-15T18:47:04.242904668Z [2026-03-15 18:47:04 +0000] [66] [INFO] Booting worker with pid: 66
2026-03-15T18:47:04.319097402Z [2026-03-15 18:47:04 +0000] [67] [INFO] Booting worker with pid: 67
2026-03-15T18:47:04.716624958Z 127.0.0.1 - - [15/Mar/2026:18:47:04 +0000] "HEAD / HTTP/1.1" 302 0 "-" "Go-http-client/1.1"
2026-03-15T18:47:10.662082275Z ==> Your service is live 🎉
2026-03-15T18:47:10.856567006Z ==> 
2026-03-15T18:47:10.861481559Z ==> ///////////////////////////////////////////////////////////
2026-03-15T18:47:10.867608069Z 10.19.101.133 - - [15/Mar/2026:18:47:10 +0000] "GET / HTTP/1.1" 302 217 "-" "Go-http-client/2.0"
2026-03-15T18:47:10.874041869Z ==> 
2026-03-15T18:47:10.87759697Z ==> Available at your primary URL https://easyflex.onrender.com
2026-03-15T18:47:10.884764076Z ==> 
2026-03-15T18:47:10.886764872Z ==> ///////////////////////////////////////////////////////////
2026-03-15T18:47:11.043915545Z 10.16.171.4 - - [15/Mar/2026:18:47:11 +0000] "GET /login?next=%2F HTTP/1.1" 200 2424 "https://easyflex.onrender.com" "Go-http-client/2.0"
2026-03-15T18:48:06.558003401Z [2026-03-15 18:48:06 +0000] [55] [INFO] Handling signal: term
2026-03-15T18:48:06.562038959Z [2026-03-15 18:48:06 +0000] [102] [INFO] Worker exiting (pid: 102)
2026-03-15T18:48:06.562109631Z [2026-03-15 18:48:06 +0000] [109] [INFO] Worker exiting (pid: 109)
2026-03-15T18:48:08.584841229Z [2026-03-15 18:48:08 +0000] [55] [INFO] Shutting down: Master
2026-03-15T18:52:09.226371472Z ==> Detected service running on port 10000
2026-03-15T18:52:09.554466188Z ==> Docs on specifying a port: https://render.com/docs/web-services#port-binding
2026-03-15T18:52:54.182032021Z 10.18.137.69 - - [15/Mar/2026:18:52:54 +0000] "GET / HTTP/1.1" 302 217 "-" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:52:54.262652654Z 10.19.101.133 - - [15/Mar/2026:18:52:54 +0000] "GET /login?next=%2F HTTP/1.1" 200 2559 "-" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:52:54.34540256Z 10.18.48.194 - - [15/Mar/2026:18:52:54 +0000] "GET /static/style.css HTTP/1.1" 200 0 "https://easyflex.onrender.com/login?next=%2F" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:52:54.440736553Z 10.18.48.194 - - [15/Mar/2026:18:52:54 +0000] "GET /favicon.ico HTTP/1.1" 404 207 "https://easyflex.onrender.com/login?next=%2F" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:52:58.379975144Z 10.20.135.162 - - [15/Mar/2026:18:52:58 +0000] "POST /login?next=%2F HTTP/1.1" 302 189 "https://easyflex.onrender.com/login?next=%2F" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:52:58.456832117Z 10.18.137.69 - - [15/Mar/2026:18:52:58 +0000] "GET / HTTP/1.1" 200 2567 "https://easyflex.onrender.com/login?next=%2F" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:52:58.525897793Z 10.18.48.194 - - [15/Mar/2026:18:52:58 +0000] "GET /static/style.css HTTP/1.1" 304 0 "https://easyflex.onrender.com/" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:00.884241086Z 10.18.137.69 - - [15/Mar/2026:18:53:00 +0000] "GET /upload-pdf HTTP/1.1" 200 4064 "https://easyflex.onrender.com/" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:01.027371844Z 10.19.101.133 - - [15/Mar/2026:18:53:01 +0000] "GET /static/style.css HTTP/1.1" 304 0 "https://easyflex.onrender.com/upload-pdf" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:13.556603425Z 10.16.171.4 - - [15/Mar/2026:18:53:13 +0000] "POST /upload-pdf HTTP/1.1" 302 209 "https://easyflex.onrender.com/upload-pdf" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:13.716793611Z 10.18.137.69 - - [15/Mar/2026:18:53:13 +0000] "GET /results/72 HTTP/1.1" 200 12569 "https://easyflex.onrender.com/upload-pdf" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:13.790603466Z 10.18.137.69 - - [15/Mar/2026:18:53:13 +0000] "GET /static/style.css HTTP/1.1" 304 0 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:13.895158018Z 10.20.135.162 - - [15/Mar/2026:18:53:13 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:16.884795158Z 10.23.219.133 - - [15/Mar/2026:18:53:16 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:19.891765244Z 10.20.135.162 - - [15/Mar/2026:18:53:19 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:22.89597034Z 10.23.219.133 - - [15/Mar/2026:18:53:22 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:26.031965032Z 10.18.48.194 - - [15/Mar/2026:18:53:26 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:28.885370945Z 10.18.137.69 - - [15/Mar/2026:18:53:28 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:31.976636142Z 10.22.46.139 - - [15/Mar/2026:18:53:31 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:34.237596201Z Model gpt-5 nepodporuje temperature/top_p (segmentace), opakuji bez nich po 1.3s
2026-03-15T18:53:34.419430569Z Model gpt-5 nepodporuje temperature/top_p (segmentace), opakuji bez nich po 1.2s
2026-03-15T18:53:34.867861659Z 10.18.137.69 - - [15/Mar/2026:18:53:34 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:37.861112124Z 10.18.137.69 - - [15/Mar/2026:18:53:37 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:38.445563405Z Segmentace selhala pro stránku 0
2026-03-15T18:53:38.445607736Z Traceback (most recent call last):
2026-03-15T18:53:38.445612696Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 563, in _seg_task
2026-03-15T18:53:38.445617306Z     payload = await self._call_openai_segment_page(b64img)
2026-03-15T18:53:38.445620956Z               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-03-15T18:53:38.445625316Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 501, in _call_openai_segment_page
2026-03-15T18:53:38.445628996Z     return self._try_parse_json_payload(content)
2026-03-15T18:53:38.445632796Z            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^
2026-03-15T18:53:38.445645477Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 1038, in _try_parse_json_payload
2026-03-15T18:53:38.445649277Z     raise json.JSONDecodeError("Unable to parse JSON from content", content, 0)
2026-03-15T18:53:38.445666488Z json.decoder.JSONDecodeError: Unable to parse JSON from content: line 1 column 1 (char 0)
2026-03-15T18:53:39.235114112Z Segmentace selhala pro stránku 1
2026-03-15T18:53:39.235130422Z Traceback (most recent call last):
2026-03-15T18:53:39.235133493Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 563, in _seg_task
2026-03-15T18:53:39.235136423Z     payload = await self._call_openai_segment_page(b64img)
2026-03-15T18:53:39.235139133Z               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-03-15T18:53:39.235142043Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 501, in _call_openai_segment_page
2026-03-15T18:53:39.235144683Z     return self._try_parse_json_payload(content)
2026-03-15T18:53:39.235147313Z            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^
2026-03-15T18:53:39.235150023Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 1038, in _try_parse_json_payload
2026-03-15T18:53:39.235152643Z     raise json.JSONDecodeError("Unable to parse JSON from content", content, 0)
2026-03-15T18:53:39.235155493Z json.decoder.JSONDecodeError: Unable to parse JSON from content: line 1 column 1 (char 0)
2026-03-15T18:53:39.605985568Z Model gpt-5 nepodporuje temperature/top_p (segmentace), opakuji bez nich po 1.1s
2026-03-15T18:53:39.713203318Z Model gpt-5 nepodporuje temperature/top_p (segmentace), opakuji bez nich po 1.1s
2026-03-15T18:53:40.875817565Z 10.22.46.139 - - [15/Mar/2026:18:53:40 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:43.436514714Z Segmentace selhala pro stránku 2
2026-03-15T18:53:43.436533314Z Traceback (most recent call last):
2026-03-15T18:53:43.436536874Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 563, in _seg_task
2026-03-15T18:53:43.436542355Z     payload = await self._call_openai_segment_page(b64img)
2026-03-15T18:53:43.436544595Z               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-03-15T18:53:43.436547455Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 501, in _call_openai_segment_page
2026-03-15T18:53:43.436549705Z     return self._try_parse_json_payload(content)
2026-03-15T18:53:43.436552015Z            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^
2026-03-15T18:53:43.436554235Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 1038, in _try_parse_json_payload
2026-03-15T18:53:43.436556505Z     raise json.JSONDecodeError("Unable to parse JSON from content", content, 0)
2026-03-15T18:53:43.436558745Z json.decoder.JSONDecodeError: Unable to parse JSON from content: line 1 column 1 (char 0)
2026-03-15T18:53:43.604960631Z Segmentace selhala pro stránku 3
2026-03-15T18:53:43.604982661Z Traceback (most recent call last):
2026-03-15T18:53:43.604986021Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 563, in _seg_task
2026-03-15T18:53:43.604988701Z     payload = await self._call_openai_segment_page(b64img)
2026-03-15T18:53:43.604990852Z               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-03-15T18:53:43.604993612Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 501, in _call_openai_segment_page
2026-03-15T18:53:43.604995972Z     return self._try_parse_json_payload(content)
2026-03-15T18:53:43.604998072Z            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^
2026-03-15T18:53:43.605000212Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 1038, in _try_parse_json_payload
2026-03-15T18:53:43.605002332Z     raise json.JSONDecodeError("Unable to parse JSON from content", content, 0)
2026-03-15T18:53:43.605004562Z json.decoder.JSONDecodeError: Unable to parse JSON from content: line 1 column 1 (char 0)
2026-03-15T18:53:43.862362805Z 10.16.171.4 - - [15/Mar/2026:18:53:43 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:43.973288132Z Model gpt-5 nepodporuje temperature/top_p (segmentace), opakuji bez nich po 1.2s
2026-03-15T18:53:45.482524958Z Model gpt-5 nepodporuje temperature/top_p (segmentace), opakuji bez nich po 1.2s
2026-03-15T18:53:46.864801857Z 10.23.219.133 - - [15/Mar/2026:18:53:46 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:48.106348028Z Segmentace selhala pro stránku 4
2026-03-15T18:53:48.106368329Z Traceback (most recent call last):
2026-03-15T18:53:48.106371299Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 563, in _seg_task
2026-03-15T18:53:48.106373769Z     payload = await self._call_openai_segment_page(b64img)
2026-03-15T18:53:48.106375609Z               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-03-15T18:53:48.106378119Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 501, in _call_openai_segment_page
2026-03-15T18:53:48.106380169Z     return self._try_parse_json_payload(content)
2026-03-15T18:53:48.106382279Z            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^
2026-03-15T18:53:48.106385169Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 1038, in _try_parse_json_payload
2026-03-15T18:53:48.10638847Z     raise json.JSONDecodeError("Unable to parse JSON from content", content, 0)
2026-03-15T18:53:48.106391579Z json.decoder.JSONDecodeError: Unable to parse JSON from content: line 1 column 1 (char 0)
2026-03-15T18:53:49.043418852Z Model gpt-5 nepodporuje temperature/top_p (segmentace), opakuji bez nich po 1.3s
2026-03-15T18:53:49.873145472Z 10.18.48.194 - - [15/Mar/2026:18:53:49 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:50.754988204Z Segmentace selhala pro stránku 5
2026-03-15T18:53:50.755021254Z Traceback (most recent call last):
2026-03-15T18:53:50.755026815Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 563, in _seg_task
2026-03-15T18:53:50.755031905Z     payload = await self._call_openai_segment_page(b64img)
2026-03-15T18:53:50.755037075Z               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-03-15T18:53:50.755043145Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 501, in _call_openai_segment_page
2026-03-15T18:53:50.755047615Z     return self._try_parse_json_payload(content)
2026-03-15T18:53:50.755052475Z            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^
2026-03-15T18:53:50.755056646Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 1038, in _try_parse_json_payload
2026-03-15T18:53:50.755059626Z     raise json.JSONDecodeError("Unable to parse JSON from content", content, 0)
2026-03-15T18:53:50.755077016Z json.decoder.JSONDecodeError: Unable to parse JSON from content: line 1 column 1 (char 0)
2026-03-15T18:53:51.494339806Z Model gpt-5 nepodporuje temperature/top_p (segmentace), opakuji bez nich po 1.2s
2026-03-15T18:53:52.900627296Z 10.18.137.69 - - [15/Mar/2026:18:53:52 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:53.780285644Z Segmentace selhala pro stránku 6
2026-03-15T18:53:53.780334005Z Traceback (most recent call last):
2026-03-15T18:53:53.780338265Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 563, in _seg_task
2026-03-15T18:53:53.780341765Z     payload = await self._call_openai_segment_page(b64img)
2026-03-15T18:53:53.780344705Z               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-03-15T18:53:53.780347885Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 501, in _call_openai_segment_page
2026-03-15T18:53:53.780350776Z     return self._try_parse_json_payload(content)
2026-03-15T18:53:53.780353816Z            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^
2026-03-15T18:53:53.780356816Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 1038, in _try_parse_json_payload
2026-03-15T18:53:53.780359596Z     raise json.JSONDecodeError("Unable to parse JSON from content", content, 0)
2026-03-15T18:53:53.780362186Z json.decoder.JSONDecodeError: Unable to parse JSON from content: line 1 column 1 (char 0)
2026-03-15T18:53:54.265366563Z Model gpt-5 nepodporuje temperature/top_p (segmentace), opakuji bez nich po 1.3s
2026-03-15T18:53:55.855907682Z 10.23.219.133 - - [15/Mar/2026:18:53:55 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:56.283679709Z Segmentace selhala pro stránku 7
2026-03-15T18:53:56.283696409Z Traceback (most recent call last):
2026-03-15T18:53:56.283702199Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 563, in _seg_task
2026-03-15T18:53:56.28370776Z     payload = await self._call_openai_segment_page(b64img)
2026-03-15T18:53:56.28371215Z               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-03-15T18:53:56.28371722Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 501, in _call_openai_segment_page
2026-03-15T18:53:56.28372195Z     return self._try_parse_json_payload(content)
2026-03-15T18:53:56.28372515Z            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^
2026-03-15T18:53:56.28372873Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 1038, in _try_parse_json_payload
2026-03-15T18:53:56.28373382Z     raise json.JSONDecodeError("Unable to parse JSON from content", content, 0)
2026-03-15T18:53:56.283738181Z json.decoder.JSONDecodeError: Unable to parse JSON from content: line 1 column 1 (char 0)
2026-03-15T18:53:58.095751144Z Model gpt-5 nepodporuje temperature/top_p (segmentace), opakuji bez nich po 1.1s
2026-03-15T18:53:58.720543682Z Segmentace selhala pro stránku 8
2026-03-15T18:53:58.720564533Z Traceback (most recent call last):
2026-03-15T18:53:58.720570263Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 563, in _seg_task
2026-03-15T18:53:58.720574953Z     payload = await self._call_openai_segment_page(b64img)
2026-03-15T18:53:58.720579493Z               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-03-15T18:53:58.720584833Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 501, in _call_openai_segment_page
2026-03-15T18:53:58.720589164Z     return self._try_parse_json_payload(content)
2026-03-15T18:53:58.720594013Z            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^
2026-03-15T18:53:58.720599044Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 1038, in _try_parse_json_payload
2026-03-15T18:53:58.720603924Z     raise json.JSONDecodeError("Unable to parse JSON from content", content, 0)
2026-03-15T18:53:58.720608204Z json.decoder.JSONDecodeError: Unable to parse JSON from content: line 1 column 1 (char 0)
2026-03-15T18:53:58.863242128Z 10.18.137.69 - - [15/Mar/2026:18:53:58 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:53:59.236681058Z Model gpt-5 nepodporuje temperature/top_p (segmentace), opakuji bez nich po 1.3s
2026-03-15T18:54:01.856519273Z 10.22.46.139 - - [15/Mar/2026:18:54:01 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:02.159010633Z Segmentace selhala pro stránku 9
2026-03-15T18:54:02.159030993Z Traceback (most recent call last):
2026-03-15T18:54:02.159036573Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 563, in _seg_task
2026-03-15T18:54:02.159041603Z     payload = await self._call_openai_segment_page(b64img)
2026-03-15T18:54:02.159045654Z               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-03-15T18:54:02.159050214Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 501, in _call_openai_segment_page
2026-03-15T18:54:02.159054104Z     return self._try_parse_json_payload(content)
2026-03-15T18:54:02.159058394Z            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^
2026-03-15T18:54:02.159082304Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 1038, in _try_parse_json_payload
2026-03-15T18:54:02.159090485Z     raise json.JSONDecodeError("Unable to parse JSON from content", content, 0)
2026-03-15T18:54:02.159094635Z json.decoder.JSONDecodeError: Unable to parse JSON from content: line 1 column 1 (char 0)
2026-03-15T18:54:02.850284141Z Model gpt-5 nepodporuje temperature/top_p (segmentace), opakuji bez nich po 1.2s
2026-03-15T18:54:04.550187942Z Segmentace selhala pro stránku 10
2026-03-15T18:54:04.550209953Z Traceback (most recent call last):
2026-03-15T18:54:04.550215933Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 563, in _seg_task
2026-03-15T18:54:04.550221013Z     payload = await self._call_openai_segment_page(b64img)
2026-03-15T18:54:04.550225303Z               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-03-15T18:54:04.550230163Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 501, in _call_openai_segment_page
2026-03-15T18:54:04.550234773Z     return self._try_parse_json_payload(content)
2026-03-15T18:54:04.550239033Z            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^
2026-03-15T18:54:04.550243343Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 1038, in _try_parse_json_payload
2026-03-15T18:54:04.550247583Z     raise json.JSONDecodeError("Unable to parse JSON from content", content, 0)
2026-03-15T18:54:04.550252214Z json.decoder.JSONDecodeError: Unable to parse JSON from content: line 1 column 1 (char 0)
2026-03-15T18:54:04.868521614Z 10.19.101.133 - - [15/Mar/2026:18:54:04 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:05.080144371Z Model gpt-5 nepodporuje temperature/top_p (segmentace), opakuji bez nich po 1.2s
2026-03-15T18:54:07.215440702Z Segmentace selhala pro stránku 11
2026-03-15T18:54:07.215461952Z Traceback (most recent call last):
2026-03-15T18:54:07.215465902Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 563, in _seg_task
2026-03-15T18:54:07.215469713Z     payload = await self._call_openai_segment_page(b64img)
2026-03-15T18:54:07.215472713Z               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-03-15T18:54:07.215476303Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 501, in _call_openai_segment_page
2026-03-15T18:54:07.215479193Z     return self._try_parse_json_payload(content)
2026-03-15T18:54:07.215492313Z            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^
2026-03-15T18:54:07.215494233Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 1038, in _try_parse_json_payload
2026-03-15T18:54:07.215496033Z     raise json.JSONDecodeError("Unable to parse JSON from content", content, 0)
2026-03-15T18:54:07.215497803Z json.decoder.JSONDecodeError: Unable to parse JSON from content: line 1 column 1 (char 0)
2026-03-15T18:54:07.868234457Z 10.18.137.69 - - [15/Mar/2026:18:54:07 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:09.10711677Z Segmentace selhala pro stránku 12
2026-03-15T18:54:09.107144941Z Traceback (most recent call last):
2026-03-15T18:54:09.107148621Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 563, in _seg_task
2026-03-15T18:54:09.107152191Z     payload = await self._call_openai_segment_page(b64img)
2026-03-15T18:54:09.107154451Z               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-03-15T18:54:09.107157072Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 501, in _call_openai_segment_page
2026-03-15T18:54:09.107159181Z     return self._try_parse_json_payload(content)
2026-03-15T18:54:09.107161472Z            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^
2026-03-15T18:54:09.107163622Z   File "/opt/render/project/src/EasyFlex/extractor.py", line 1038, in _try_parse_json_payload
2026-03-15T18:54:09.107165982Z     raise json.JSONDecodeError("Unable to parse JSON from content", content, 0)
2026-03-15T18:54:09.107168192Z json.decoder.JSONDecodeError: Unable to parse JSON from content: line 1 column 1 (char 0)
2026-03-15T18:54:09.107239744Z Segmentace nenašla žádné skupiny, fallback na single extrakci: /tmp/easyflex-pdf-batch-3-42zrkyle/INV-2025_0201-0228.pdf
2026-03-15T18:54:10.886287075Z 10.19.101.133 - - [15/Mar/2026:18:54:10 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:13.889483609Z 10.23.175.65 - - [15/Mar/2026:18:54:13 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:16.881909029Z 10.23.219.133 - - [15/Mar/2026:18:54:16 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:19.885151614Z 10.18.137.69 - - [15/Mar/2026:18:54:19 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:22.856844747Z 10.18.137.69 - - [15/Mar/2026:18:54:22 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:24.429027562Z Model gpt-5 nepodporuje temperature/top_p, opakuji bez těchto parametrů (attempt 1/6) po 1.1s
2026-03-15T18:54:25.938960359Z 10.23.175.65 - - [15/Mar/2026:18:54:25 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:28.877271168Z 10.23.175.65 - - [15/Mar/2026:18:54:28 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:31.857621156Z 10.18.137.69 - - [15/Mar/2026:18:54:31 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:34.868982821Z 10.22.46.139 - - [15/Mar/2026:18:54:34 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:37.853952043Z 10.18.48.194 - - [15/Mar/2026:18:54:37 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:40.864850264Z 10.22.46.139 - - [15/Mar/2026:18:54:40 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:43.847941832Z Nepodařilo se zpracovat JSON výstup: Unable to parse JSON from content: line 1 column 1 (char 0)
2026-03-15T18:54:43.847973493Z Výstup utnut kvůli limitu tokenů (attempt 2/6), zvyšuji na 2048 a čekám 2.6s
2026-03-15T18:54:43.866267998Z 10.18.137.69 - - [15/Mar/2026:18:54:43 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:46.926037188Z 10.18.137.69 - - [15/Mar/2026:18:54:46 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:49.854666492Z 10.18.137.69 - - [15/Mar/2026:18:54:49 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:52.862923525Z 10.18.137.69 - - [15/Mar/2026:18:54:52 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:55.863607487Z 10.18.137.69 - - [15/Mar/2026:18:54:55 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:54:58.865275797Z 10.18.137.69 - - [15/Mar/2026:18:54:58 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:55:01.895311886Z 10.22.46.139 - - [15/Mar/2026:18:55:01 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:55:04.880716101Z 10.18.48.194 - - [15/Mar/2026:18:55:04 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:55:07.876345664Z 10.18.137.69 - - [15/Mar/2026:18:55:07 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:55:10.872672467Z 10.18.137.69 - - [15/Mar/2026:18:55:10 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:55:13.91719586Z 10.18.137.69 - - [15/Mar/2026:18:55:13 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:55:16.91994071Z 10.18.137.69 - - [15/Mar/2026:18:55:16 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:55:19.937609247Z 10.18.137.69 - - [15/Mar/2026:18:55:19 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:55:21.409210732Z Nepodařilo se zpracovat JSON výstup: Unable to parse JSON from content: line 1 column 1 (char 0)
2026-03-15T18:55:21.409243523Z Výstup utnut kvůli limitu tokenů (attempt 3/6), zvyšuji na 4096 a čekám 5.0s
2026-03-15T18:55:22.917260922Z 10.18.137.69 - - [15/Mar/2026:18:55:22 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:55:25.940285996Z 10.23.175.65 - - [15/Mar/2026:18:55:25 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:55:28.914487031Z 10.23.175.65 - - [15/Mar/2026:18:55:28 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:55:32.000355412Z 10.18.48.194 - - [15/Mar/2026:18:55:31 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:55:34.915035276Z 10.18.137.69 - - [15/Mar/2026:18:55:34 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
2026-03-15T18:55:37.928586722Z 10.23.175.65 - - [15/Mar/2026:18:55:37 +0000] "GET /results/72/progress HTTP/1.1" 200 431 "https://easyflex.onrender.com/results/72" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"