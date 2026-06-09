import XCTest

@testable import DotsTTS

/// The language-tag resolver maps user input to the `[CODE]` content the runtime
/// prefixes to the text. Off (nil) by default; explicit codes/names resolve;
/// Cantonese maps to the accent tag; auto_detect is a coarse CJK heuristic.
final class DotsLanguageTagTests: XCTestCase {
    func testNoTagByDefault() {
        XCTAssertNil(DotsLanguageTag.code(for: nil, text: "hello"))
        XCTAssertNil(DotsLanguageTag.code(for: "", text: "hello"))
        XCTAssertNil(DotsLanguageTag.code(for: "none", text: "hello"))
        XCTAssertNil(DotsLanguageTag.code(for: "  None  ", text: "hello"))
    }

    func testExplicitCodesAndNames() {
        XCTAssertEqual(DotsLanguageTag.code(for: "EN", text: "hi"), "EN")
        XCTAssertEqual(DotsLanguageTag.code(for: "english", text: "hi"), "EN")
        XCTAssertEqual(DotsLanguageTag.code(for: "zh", text: "hi"), "ZH")
        XCTAssertEqual(DotsLanguageTag.code(for: "mandarin", text: "hi"), "ZH")
        XCTAssertEqual(DotsLanguageTag.code(for: "japanese", text: "hi"), "JA")
    }

    func testCantoneseMapsToAccentTag() {
        XCTAssertEqual(DotsLanguageTag.code(for: "yue", text: "hi"), "口音:粤语")
        XCTAssertEqual(DotsLanguageTag.code(for: "cantonese", text: "hi"), "口音:粤语")
        // An already-formed accent tag passes through unchanged.
        XCTAssertEqual(DotsLanguageTag.code(for: "口音:粤语", text: "hi"), "口音:粤语")
    }

    func testAutoDetectHeuristic() {
        XCTAssertEqual(DotsLanguageTag.code(for: "auto_detect", text: "hello world"), "EN")
        XCTAssertEqual(DotsLanguageTag.code(for: "auto_detect", text: "你好世界"), "ZH")
    }

    func testBareIsoCodePassesThrough() {
        XCTAssertEqual(DotsLanguageTag.code(for: "de", text: "hi"), "DE")
        XCTAssertEqual(DotsLanguageTag.code(for: "fra", text: "hi"), "FRA")
        // Not a plausible code: rejected.
        XCTAssertNil(DotsLanguageTag.code(for: "klingon", text: "hi"))
    }
}
