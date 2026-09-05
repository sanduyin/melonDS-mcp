// Genuine bounded C decompilation for melonDS MCP. GPL-3.0-or-later.
// @category melonDS.MCP
// @runtime Java

import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.framework.Application;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressRange;
import ghidra.program.model.address.AddressRangeIterator;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.block.BasicBlockModel;
import ghidra.program.model.block.CodeBlock;
import ghidra.program.model.block.CodeBlockIterator;
import ghidra.program.model.block.CodeBlockReference;
import ghidra.program.model.block.CodeBlockReferenceIterator;
import ghidra.program.model.lang.Register;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.mem.MemoryBlock;

public class McpDecompile extends GhidraScript {
    private static final int MAX_C = 131072;
    private static String address(Address value) {
        return String.format("0x%08x", value.getOffset());
    }

    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length != 4) throw new IllegalArgumentException("Expected output, entry, thumb, timeout");
        Path output = Path.of(args[0]);
        JsonObject result = new JsonObject();
        DecompInterface decompiler = new DecompInterface();
        try {
            if (currentProgram == null) throw new IllegalStateException("No imported program");
            Address entry = toAddr(Long.decode(args[1]));
            boolean thumb = Boolean.parseBoolean(args[2]);
            int timeout = Integer.parseInt(args[3]);
            Address start = currentProgram.getMinAddress();
            Address end = currentProgram.getMaxAddress();
            AddressSet supplied = new AddressSet(start, end);
            if (!supplied.contains(entry)) throw new IllegalArgumentException("Entry is outside input bytes");
            for (MemoryBlock block : currentProgram.getMemory().getBlocks()) block.setExecute(true);
            Register tmode = currentProgram.getRegister("TMode");
            if (tmode == null) throw new IllegalStateException("ARM language has no TMode context");
            currentProgram.getProgramContext().setValue(tmode, start, end,
                thumb ? BigInteger.ONE : BigInteger.ZERO);
            DisassembleCommand command = new DisassembleCommand(entry, supplied, true);
            command.enableCodeAnalysis(false);
            if (!command.applyTo(currentProgram, monitor))
                throw new IllegalStateException("Disassembly failed: " + command.getStatusMsg());
            Function function = createFunction(entry, "mcp_entry");
            if (function == null) function = getFunctionAt(entry);
            if (function == null) throw new IllegalStateException("Could not create entry function");
            decompiler.toggleCCode(true);
            decompiler.toggleSyntaxTree(true);
            decompiler.setSimplificationStyle("decompile");
            if (!decompiler.openProgram(currentProgram))
                throw new IllegalStateException("Cannot start decompiler: " + decompiler.getLastMessage());
            DecompileResults decoded = decompiler.decompileFunction(function, timeout, monitor);
            if (decoded == null || !decoded.decompileCompleted() || decoded.getDecompiledFunction() == null)
                throw new IllegalStateException(decoded == null ? "No decompiler result" : decoded.getErrorMessage());
            String c = decoded.getDecompiledFunction().getC();
            result.addProperty("ok", true);
            result.addProperty("c_code", c.length() > MAX_C ? c.substring(0, MAX_C) : c);
            result.addProperty("c_code_truncated", c.length() > MAX_C);
            JsonObject analyzer = new JsonObject();
            analyzer.addProperty("name", "Ghidra");
            analyzer.addProperty("version", Application.getApplicationVersion());
            analyzer.addProperty("language_id", currentProgram.getLanguageID().toString());
            analyzer.addProperty("compiler_spec", currentProgram.getCompilerSpec().getCompilerSpecID().toString());
            analyzer.addProperty("decompiler_version", decompiler.getMajorVersion() + "." + decompiler.getMinorVersion());
            result.add("analyzer", analyzer);
            JsonObject meta = new JsonObject();
            meta.addProperty("name", function.getName());
            meta.addProperty("entry_address", address(function.getEntryPoint()));
            meta.addProperty("signature", decoded.getDecompiledFunction().getSignature());
            JsonArray ranges = new JsonArray();
            AddressRangeIterator iterator = function.getBody().getAddressRanges();
            while (iterator.hasNext()) {
                AddressRange range = iterator.next();
                JsonObject row = new JsonObject();
                row.addProperty("start", address(range.getMinAddress()));
                row.addProperty("end", address(range.getMaxAddress()));
                ranges.add(row);
            }
            meta.add("body_ranges", ranges);
            int instructions = 0;
            InstructionIterator listing = currentProgram.getListing().getInstructions(function.getBody(), true);
            while (listing.hasNext()) { listing.next(); instructions++; }
            meta.addProperty("instruction_count", instructions);
            result.add("function", meta);
            result.add("cfg", cfg(function));
            JsonArray diagnostics = new JsonArray();
            String error = decoded.getErrorMessage();
            if (error != null && !error.isBlank()) diagnostics.add(error.substring(0, Math.min(error.length(), 8192)));
            result.add("diagnostics", diagnostics);
        } catch (Exception exception) {
            result = new JsonObject();
            result.addProperty("ok", false);
            String error = exception.getClass().getSimpleName() + ": " + exception.getMessage();
            result.addProperty("error", error.substring(0, Math.min(error.length(), 8192)));
        } finally {
            decompiler.dispose();
        }
        Files.writeString(output, new Gson().toJson(result), StandardCharsets.UTF_8);
    }

    private JsonObject cfg(Function function) throws Exception {
        JsonObject graph = new JsonObject();
        JsonArray nodes = new JsonArray();
        JsonArray edges = new JsonArray();
        boolean truncated = false;
        BasicBlockModel model = new BasicBlockModel(currentProgram);
        CodeBlockIterator blocks = model.getCodeBlocksContaining(function.getBody(), monitor);
        while (blocks.hasNext()) {
            monitor.checkCancelled();
            if (nodes.size() >= 256) { truncated = true; break; }
            CodeBlock block = blocks.next();
            JsonObject node = new JsonObject();
            node.addProperty("start", address(block.getMinAddress()));
            node.addProperty("end", address(block.getMaxAddress()));
            nodes.add(node);
            CodeBlockReferenceIterator outgoing = block.getDestinations(monitor);
            while (outgoing.hasNext()) {
                if (edges.size() >= 1024) { truncated = true; break; }
                CodeBlockReference reference = outgoing.next();
                JsonObject edge = new JsonObject();
                edge.addProperty("source", address(block.getMinAddress()));
                edge.addProperty("target", address(reference.getDestinationAddress()));
                edge.addProperty("type", reference.getFlowType().toString());
                edge.addProperty("external", !function.getBody().contains(reference.getDestinationAddress()));
                edges.add(edge);
            }
        }
        graph.add("nodes", nodes);
        graph.add("edges", edges);
        graph.addProperty("truncated", truncated);
        return graph;
    }
}
