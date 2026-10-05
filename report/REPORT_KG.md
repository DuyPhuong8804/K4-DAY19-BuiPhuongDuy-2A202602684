# Báo cáo Day 19: Flat RAG vs GraphRAG

**Họ tên:** Bùi Phương Duy  **MSSV:** 2A202602684  **Ngày:** 2026-10-05

> Kỳ vọng và thang điểm: `SUBMISSION.md`. Mọi số liệu phải khớp với `ket_qua_benchmark_kg.txt`. Bản thiết kế ontology nộp riêng ở `report/ONTOLOGY.md`.

Cấu hình chạy: chat `openai:gpt-4o-mini`, embedding `openai:text-embedding-3-small`, top_k=3, chunk_size=800, 176 chunk. Graph của lần benchmark: 317 node / 749 cạnh, dựng bằng ontology tự thiết kế (xem `ONTOLOGY.md`). Mỗi pipeline chỉ chạy một lần.

## 1. Chi phí (10 điểm)

```
== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176     56072        0   0.00112     42.4
graph       196     94938     5851   0.01046    107.0

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.43   1.17      694       47   0.00013     1.36
graph       1.00   1.83     3368       80   0.00055     2.11
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| Indexing USD | 0,00112 | 0,01046 | ×9,3 |
| Indexing giây | 42,4 | 107,0 | ×2,5 |
| Mỗi câu: USD | 0,00013 | 0,00055 | ×4,2 |
| Mỗi câu: giây | 1,36 | 2,11 | ×1,6 |
| Mỗi câu: in_tok | 694 | 3.368 | ×4,9 |

**Chi phí tăng thêm đến từ đâu?**
> Lúc dựng: Graph gọi LLM thêm đúng 20 lần (196 − 176 lần gọi), mỗi lần cho một bài báo. Chúng thêm 38.866 token vào (94.938 − 56.072) và 5.851 token ra, tương ứng khoảng 0,0093 USD; phần luật dựng bằng regex nên không tốn token. Lúc trả lời: chi phí tăng do prompt dài hơn, vì ngoài 3 chunk Graph còn nhận thêm các dữ kiện từ graph (tóm tắt vụ, các khoản luật có đủ văn bản), nên token vào tăng từ 694 lên 3.368 trong khi token ra gần như không đổi (47 so với 80).
>
> Về điểm hòa vốn: không có điểm hòa vốn theo tiền, vì Graph đắt hơn ở cả hai pha (cho 6 câu hỏi: ≈ 0,0138 USD so với ≈ 0,0019 USD, ×7,2). Vì vậy phép so sánh nên dựa vào việc Graph trả lời được những câu Flat không trả lời được (mục 2): Flat nói "Không đủ thông tin" ở Q3 và Q4.

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao (1 câu) |
| --- | --- | --- | --- | --- | --- |
| Q1 | single-hop-law | 1,00 / 2 | 1,00 / 2 | Hòa | Đáp án nằm gọn trong một khoản luật nên vector search đã đủ |
| Q2 | single-hop-news | 1,00 / 2 | 1,00 / 2 | Hòa | Đáp án nằm trong một bài báo nên vector search đã đủ |
| Q3 | cross-kb | 0,00 / 0 | 1,00 / 2 | Graph | Tên bị cáo và mức án ở tin, Điều 251 và khung hình phạt ở luật; Flat trả lời "Không đủ thông tin" |
| Q4 | cross-kb | 0,00 / 0 | 1,00 / 2 | Graph | Flat không có đoạn nào chứa cả "Hoàng Nato" lẫn khung hình phạt; Graph đi từ người tới Điều 255 và lấy khoản nặng nhất (`severity`) |
| Q5 | cross-kb-multi-hop | 0,60 / 1 | 1,00 / 2 | Graph | Flat chọn "khoản b" (sai), Graph so 9.600 g với ngưỡng và chọn đúng khoản 4 Điều 250 |
| Q6 | aggregation | 0,00 / 2 | 1,00 / 1 | Graph (xem lỗi E4) | Graph liệt kê đủ ba vụ trong đáp án chuẩn; recall và judge mâu thuẫn nhau ở cả hai bên |

Quy luật rút ra: câu hỏi mà đáp án nằm trong một KB (Q1, Q2) thì hai pipeline bằng nhau, Graph chỉ tốn thêm token. Câu cần nối hai KB (Q3, Q4, Q5) thì Flat trượt (recall 0,00 và 0,60) còn Graph đạt 1,00. Câu tổng hợp (Q6) thắng nhờ cạnh `INVOLVES` tới `Substance`, nhưng đây là câu mà phép đo kém tin cậy nhất.

## 3. Phân tích lỗi (20 điểm)

### Lỗi E2: Thiếu ngữ cảnh luật (câu hỏi "phạt tối đa")

- **Hiện tượng:** với ontology gợi ý, Graph trả lời sai mức phạt tối đa ở Q4 dù graph có đủ Điều 255.
- **Bằng chứng:** câu trả lời Q4 của GraphRAG trong `ket_qua_benchmark_kg.hint.txt` (chạy trên ontology gợi ý), recall 0,67, judge 1:

```
Giang hồ 'Hoàng Nato' bị bắt về hành vi tổ chức sử dụng trái phép chất ma túy. Hành vi này có thể bị phạt tù tối đa 7 năm theo Điều 255 BLHS khoản 1.
```

Đáp án chuẩn là "khung cao nhất là tù 20 năm hoặc tù chung thân". Graph có dữ liệu đúng, kiểm tra bằng Cypher:

```cypher
MATCH (a:Article {id:'Điều 255 BLHS'})-[:HAS_CLAUSE]->(c:Clause)
RETURN c.number AS khoan, c.severity AS severity, c.penalty AS penalty ORDER BY severity DESC
```

```
khoan=4  severity=100  penalty="phạt tù 20 năm hoặc tù chung thân"
khoan=3  severity=20   penalty="phạt tù từ 15 năm đến 20 năm"
khoan=2  severity=15   penalty="phạt tù từ 07 năm đến 15 năm"
khoan=1  severity=7    penalty="phạt tù từ 02 năm đến 07 năm"
khoan=5  severity=0    penalty="phạt tiền từ 50.000.000 đồng đến 500.000.000 đồng, ..."
```

Sau khi sửa (ontology của tôi, `ket_qua_benchmark_kg.txt`), Q4 của Graph: *"…có thể bị phạt tù tối đa 20 năm hoặc tù chung thân theo Điều 255 Bộ luật Hình sự…"*, recall 1,00, judge 2.

- **Nguyên nhân:** ở bước Cypher (KG-3). Quy tắc lọc khoản của gợi ý chỉ giữ khoản 1 và các khoản `MENTIONS` chất mà vụ có. Điều 255 có khoản 1 không nhắc chất, và khung nặng nhất (khoản 4) không thỏa quy tắc nào, nên không bao giờ vào prompt. Trong quá trình sửa tôi còn gặp nguyên nhân thứ hai: vector search trả về chunk của Điều 249/250/252, các node `Clause` của chúng thành seed, và `seed_facts` liệt kê tới 60 cạnh cấu trúc `HAS_CLAUSE`/`MENTIONS` đẩy các dữ kiện cần thiết ra khỏi prompt. Chỉ thêm `severity` mà chưa xử lý lỗi này thì Q4 vẫn trượt (recall 0,33 và 0,00 ở hai lần chạy trung gian).
- **Đề xuất sửa:** đã làm trong `src/graph.py`: (1) thêm `Clause.severity` (tử hình 1000, chung thân 100, hoặc số năm tù lớn nhất) và khi câu hỏi chứa "tối đa / cao nhất / nặng nhất" thì lấy khoản `ORDER BY severity DESC LIMIT 1`; (2) bỏ các cạnh cấu trúc của `Clause` khỏi dữ kiện seed. Đánh đổi: thêm một khoản (khoảng 100 đến 200 token) cho mỗi câu hỏi loại này; nhận diện bằng từ khóa nên sẽ sót cách diễn đạt khác ("bao nhiêu năm tù là nhiều nhất").

### Lỗi E4: Phép đo sai (recall và judge mâu thuẫn ở Q6)

- **Hiện tượng:** Q6 của Flat có recall 0,00 nhưng judge 2 (đúng đủ); Q6 của Graph có recall 1,00 nhưng judge chỉ 1. Hai thước đo trả về hai kết luận ngược nhau.
- **Bằng chứng:** `must_include` của Q6 trong `data/benchmark_kg.json` là `['Cái Quang Huy', 'Lê Minh Thành', 'Pháp y tâm thần']`. Câu trả lời Flat (`ket_qua_benchmark_kg.txt`, Q6 flat, recall 0,00, judge 2):

```
Cả ba vụ việc trong tin tức đều có liên quan đến ma túy MDMA. Cụ thể:
1. Vụ việc của Đức liên quan đến số viên nén hình tam giác màu hồng - xám được xác định là MDMA.
2. Vụ việc của Thành liên quan đến 5 viên nén màu trắng được xác định là ma túy MDMA.
3. Vụ việc của Đông liên quan đến 0,686g ma túy MDMA được thu giữ trong buồng chữa bệnh.
```

Câu trả lời Graph (Q6 graph, recall 1,00, judge 1) liệt kê 5 vụ, trong đó có cả ba tên cần thiết nhưng thêm "Sầm Sơn" và "Hoàng Nato (khối lượng: không rõ)". Cùng câu trả lời Flat gần như y hệt ở lần chạy gợi ý (`ket_qua_benchmark_kg.hint.txt`) lại được judge 1, không phải 2.

Bài `news-100260930085028036` (nguồn của "0,686g") có tiêu đề *"Bệnh nhân ‘tâm thần’ kể cuộc chơi ma túy trên bãi biển cùng điều dưỡng Viện Pháp y tâm thần"*, nên vụ thứ 3 của Flat ("0,686g … buồng chữa bệnh") rất có thể chính là vụ Viện Pháp y trong đáp án chuẩn, được mô tả bằng nội dung chứ không bằng tên.

- **Nguyên nhân:** ở bước phép đo. `recall` đếm từ khóa nguyên văn nên một câu trả lời đúng ý nhưng không nhắc tên riêng bị 0; câu trả lời liệt kê thừa vẫn được 1,00. `judge` do LLM chấm nên kết quả dao động giữa các lần chạy với cùng câu trả lời (1 so với 2) và hai thước đo không cùng một tiêu chí.
- **Đề xuất sửa:** (1) chấm bằng cả hai và đọc câu trả lời, như mục 2 đã làm; (2) với câu tổng hợp, thay `must_include` là chuỗi ngắn bằng một danh sách vụ chuẩn và so khớp đồng nghĩa (ví dụ "Pháp y tâm thần" hoặc "0,686g"); (3) chạy judge nhiều lần rồi lấy trung bình. Đánh đổi: tốn thêm lần gọi LLM cho judge, và phải viết đáp án chuẩn công phu hơn.

### Lỗi E3: Trùng thực thể (cùng một sự việc thành nhiều `Case`)

- **Hiện tượng:** `Case` không gộp được giữa các bài, nên cùng một sự việc xuất hiện nhiều lần và làm câu tổng hợp liệt kê trùng hoặc thừa.
- **Bằng chứng:**

```cypher
MATCH (p:Person)-[:INVOLVED_IN]->(k:Case) WHERE p.name STARTS WITH 'Dương Minh'
RETURN p.name AS p, k.id AS case_id, k.name AS k
```

```
Dương Minh Tuấn | news-100260925144412498#0 | Bắt 'Hoàng Nato' và triệt phá 8 đường dây ma túy
Dương Minh Tuấn | news-100260924095400982#0 | Vụ bắt giữ Hoàng Nato và Phan Kim Nhi
Dương Minh Tuấn | news-100260922111804786#0 | Vụ bắt giữ TikToker Phannhibeauty và giang hồ Hoàng Nato
Dương Minh Tuấn | news-100260920221957595#0 | Bắt giang hồ 'Hoàng Nato'
```

`Person` được gộp đúng (một node `Dương Minh Tuấn`, `aliases = ['Hoàng Nato']`) nhưng người đó nối tới 4 `Case` khác nhau từ 4 bài. Ở Q6 có cùng hiện tượng: `Vụ án tổ chức sử dụng ma túy tại Sầm Sơn` (bài `…930085028036`, 0,686g) và `Vụ án Viện Pháp y tâm thần` (bài `…924105118645`) cùng liên quan tới một sự việc nhưng là hai node.

- **Nguyên nhân:** ở thiết kế ontology và khóa định danh. `Case.id` = `doc_id#số thứ tự` (ổn định theo bài) nên không bao giờ gộp xuyên bài, trong khi tên vụ do LLM đặt mỗi bài một kiểu. `Person` gộp được vì khóa theo tên người, còn `Case` không có khóa tự nhiên.
- **Đề xuất sửa:** thêm bước gộp sau khi trích xuất: so sánh (tập `Person` + `Location` + `date`) giữa các `Case` và gộp khi giao nhau lớn, hoặc nhờ LLM quyết định cho từng cặp ứng viên. Đánh đổi: thêm lần gọi LLM, và gộp nhầm hai vụ khác nhau của cùng một người còn tệ hơn để trùng.

## 4. Kết luận (5 điểm)

Khi nào nên dùng KG, khi nào Flat RAG là đủ? Dẫn số liệu ở mục 1 và 2.
> Flat RAG là đủ khi đáp án nằm trong một đoạn của một nguồn: Q1 và Q2 đều đạt recall 1,00 và judge 2 ở cả hai pipeline, trong khi Graph tốn token vào gấp 4,9 lần (3.368 so với 694) và USD mỗi câu gấp 4,2 lần.
>
> KG đáng tiền khi câu hỏi phải nối thông tin từ hai nguồn qua một thực thể chung. Với Q3, Q4, Q5 Flat đạt recall 0,00, 0,00 và 0,60 (hai câu đầu trả lời "Không đủ thông tin"), còn Graph đạt 1,00 ở cả ba. Chi phí phải trả: dựng graph đắt gấp 9,3 lần (0,01046 so với 0,00112 USD) và chậm gấp 2,5 lần, và chi phí này chỉ hợp lý khi nhiều câu hỏi thuộc loại cross-kb.
>
> Điều kiện cụ thể: (1) hai KB có một thực thể chung ổn định (ở đây là tên tội); (2) một nguồn đủ đều để trích bằng regex (luật); (3) câu hỏi cross-kb đủ nhiều để bù chi phí dựng. Kết quả chỉ dựa trên một lần chạy với 6 câu và LLM trích xuất không ổn định, nên chênh lệch 0,43 so với 1,00 chỉ có tính chỉ báo; mức độ tin cậy của câu tổng hợp (Q6) còn thấp vì lỗi phép đo ở E4.

## 5. Tự kiểm (5 điểm)

```
$ pytest tests/ -q
................................................                         [100%]
48 passed in 0.10s

$ python bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = openai:gpt-4o-mini | embedding = openai:text-embedding-3-small
[OK] KG-2 build_graph: 249 node / 634 cạnh, đường xuyên 2 KB dài 2 cạnh
[OK] KG-3 context: 20 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00085. Graph nhỏ (luật + 1 bài) vẫn còn trong Neo4j để bạn xem; chạy --judge để dựng graph đầy đủ.
```

Ảnh Neo4j: `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png`.
Người đã chọn cho `kg_my_case.png`: Kim Yu Young.

## Vấn đề gặp phải (không tính điểm)

Lỗi chưa giải quyết được: không có. Lỗi đã gặp và đã giải quyết:
> - `bench_kg.py --judge` dừng với `openai.PermissionDeniedError: Error code: 403 … does not have access to model text-embedding-3-small (model_not_found)`. Nguyên nhân là project OpenAI của key chưa được cấp quyền model embedding. Sau khi bật quyền thì chạy lại thành công.
> - Số node trong ảnh Neo4j có thể lệch vài node so với `ket_qua_benchmark_kg.txt` vì graph được dựng lại bằng `--build` sau khi benchmark (LLM trích xuất mỗi lần lệch một chút).
