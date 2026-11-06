import Foundation

let keys: Set<URLResourceKey> = [
    .isUbiquitousItemKey,
    .ubiquitousItemIsUploadedKey,
    .ubiquitousItemIsUploadingKey,
    .ubiquitousItemUploadingErrorKey,
    .ubiquitousItemDownloadingStatusKey,
    .ubiquitousItemHasUnresolvedConflictsKey,
    .fileSizeKey,
]

for path in CommandLine.arguments.dropFirst() {
    let u = URL(fileURLWithPath: path)
    do {
        let v = try u.resourceValues(forKeys: keys)
        var out: [String: Any] = [
            "path": path,
            "isUbiquitous": v.isUbiquitousItem as Any,
            "isUploaded": v.ubiquitousItemIsUploaded as Any,
            "isUploading": v.ubiquitousItemIsUploading as Any,
            "hasConflicts": v.ubiquitousItemHasUnresolvedConflicts as Any,
            "downloadStatus": v.ubiquitousItemDownloadingStatus?.rawValue as Any,
            "size": v.fileSize as Any,
        ]
        if let error = v.ubiquitousItemUploadingError {
            out["uploadError"] = error.localizedDescription
            out["errorDomain"] = (error as NSError).domain
            out["errorCode"] = (error as NSError).code
        }
        out = out.mapValues { x in
            let m = Mirror(reflecting: x)
            if m.displayStyle == .optional {
                return m.children.first?.value ?? NSNull()
            }
            return x
        }
        let data = try JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
        print(String(data: data, encoding: .utf8)!)
    } catch {
        print("ERROR \(path): \(error)")
    }
}
