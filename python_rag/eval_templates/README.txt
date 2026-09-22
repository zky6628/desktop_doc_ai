评测数据集标注模板说明

一、问题集（questions.txt）
每行一个问题，空白行忽略，重复行去重。M7 要求 ≥100 条。

二、分层标注（qrels.jsonl，每行一个 JSON 对象）
必填层：
  question       问题文本（须与问题集逐字一致，脚本按文本对齐）
  relevant_docs  相关文档文件名列表（与上传到知识库的 display_name 一致）

可选层（知道就填，缺失不阻塞计算）：
  sources[].file/page/section  预期来源定位（txt/md 无页码时省略 page）
  relevance                    文档级分级：2=直接回答，1=部分相关，0=无关；
                               缺省视为 1（二元相关性）

三、指标计算口径（compute_metrics.py）
  Recall@5  按 relevant_docs 文档去重（必填层即够）
  MRR@10    首个相关命中的排名倒数（必填层即够）
  nDCG@10   有分级用分级；无分级按二元（命中=1），报告注明口径

四、导入成功率 manifest（import_success_check.py 用，CSV）
  列：file,expect_success,note
  file            样本文件相对/绝对路径
  expect_success  true=支持格式且未损坏（进成功率分母）
                  false=损坏/加密/安全拒绝预期（单独核对拒绝行为）
  note            备注（样本类型说明）
