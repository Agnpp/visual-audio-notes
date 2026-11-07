# Office 直接导入支持范围

核查日期：2026-10-31。目标是让使用者直接选取课件和文档，不再把手动导出 PDF 当作正常流程的前提。

`office_import.convert_office(source, destination, progress=None)` 读取原文件，生成 PDF 阅读预览和 `office_import.json`。转换属于无需 AI 的文件处理。原件不覆盖，图片识字、图表含义和语义归纳仍是独立模型能力。

## 当前已实现

| 格式 | 直接取得的内容 | 内置预览与定位 |
| --- | --- | --- |
| PPTX | 按演示文稿关系表顺序读取幻灯片文字、表格文本、演讲者备注及包内图片 | 按幻灯片分页面，基本文本框和图片按原位置绘制；保留幻灯片标识和备注 |
| DOCX | 正文段落、表格、包内嵌入图片 | 正文重排为阅读页，图片单独保留；记录正文区块，不把阅读页号冒充 Word 原页号 |
| XLSX | 工作表、单元格地址、共享/内联字符串、原生数值、公式与缓存值 | 按工作表生成带单元格地址的阅读页；公式不执行、不重算，缺缓存值明确说明 |
| VSDX | 绘图页、图形文本、基本位置、连接端点 | 生成基本框图阅读预览；保留页名和图形标识，不从连接端点擅自推断箭头含义 |

PPTX 备注作为**课件补充**进入内容，不能据此声称录音讲过。图片页不编造文字；用于 OCR 的源图不包含程序生成的标题和页脚，避免把界面说明混入资料证据。

这不是四种格式的完整 Office 排版引擎。内置输出明确标记为“阅读预览、不是原版排版”。PPT 母版、主题、动画、复杂组合、图表；Word 复杂公式、批注与布局；Excel 图表、透视表、日期或自定义数值格式；Visio 母版、复杂几何、箭头语义和嵌入图片，均不能仅凭这条基本解析路径宣称完全还原。完整原件始终保留。

## 转换组件

程序可探测已安装的 LibreOffice，并使用独立配置目录在后台生成 PDF；不附着或关闭使用者已打开的 Office 会话，不安装全局组件。转换器失败时，对可原生解析的 OOXML 回退为明确标记的内置阅读预览。

本轮环境没有检测到可用的 LibreOffice 或标准目录下的 PowerPoint/Word/Excel 可执行程序，因此**已测试的是内置解析和阅读预览；没有把未执行的 LibreOffice 或 Microsoft Office 自动化说成实测通过**。Microsoft Office COM 转换尚未实现，也不是内置阅读预览的依赖。

旧版二进制 `.ppt/.doc/.xls/.vsd` 无法靠 OOXML 解析。只有检测到可用转换组件时才尝试转换；否则返回明确缺项，不制造空白的成功结果，也不要求使用者在命令行安装工具。旧格式转换器的安装管理仍需产品侧另行完成。

## 处理与验证边界

- 直接读取 ZIP 包内的 XML 和资源，不解压任意路径、不执行宏，不访问外部关系链接；检测到外部链接的 OOXML 不交给外部转换器自动更新。
- 拒绝路径穿越、异常文件名、重复部件、加密、超大或高压缩比容器，以及 XML DTD/实体定义。
- 预览按页压缩并逐页写入 PDF，避免把整份长课件全部以未压缩 RGB 常驻内存。
- Windows 中文字体只用于本机栅格化预览，不重新分发系统字体文件。
- 原生文字与渲染后的 PDF 分开记录。桌面集成必须在冻结输入绑定前，将原生文字映射到相应预览页；不能因为预览 PDF 为图片就把有原生文字的 Office 重做 OCR。
- 来源只定位到能够证明的幻灯片、正文区块、工作表/单元格或绘图页/图形。重排阅读页不等于原始页码。

当前 `tests/test_office_import.py` 的 11 项测试通过，包含关系表顺序、中文文字、备注和图片保留、表格及公式、Visio 连接、有效 PDF 实际渲染、原件不变、危险容器拒绝。测试使用原创合成文件，不能代替复杂真实课件的兼容性验收。

## 官方依据

- [Microsoft：PresentationML 结构](https://learn.microsoft.com/en-us/office/open-xml/presentation/structure-of-a-presentationml-document)：演示文稿通过关系关联幻灯片、备注、图片、图表等部件。
- [Microsoft：SpreadsheetML 结构](https://learn.microsoft.com/en-us/office/open-xml/spreadsheet/structure-of-a-spreadsheetml-document)：工作表、共享字符串、公式和缓存数据属于不同内容。
- [Microsoft：VSDX 文件格式](https://learn.microsoft.com/en-us/office/client-developer/visio/introduction-to-the-visio-file-formatvsdx)：ZIP/OPC/XML 包、页面和图形关系。
- [LibreOffice：转换过滤器](https://help.libreoffice.org/latest/en-US/text/shared/guide/convertfilters.html)：命令行 PDF 导出和各文档格式过滤器。
- [Microsoft：PowerPoint PDF 导出](https://learn.microsoft.com/en-us/office/vba/api/powerpoint.presentation.exportasfixedformat)、[Word PDF 导出](https://learn.microsoft.com/en-us/office/vba/api/word.document.exportasfixedformat)、[Excel PDF 导出](https://learn.microsoft.com/en-us/office/vba/api/excel.workbook.exportasfixedformat)：后续原生转换器的官方接口依据，本轮未实现或实测这些 COM 路径。
