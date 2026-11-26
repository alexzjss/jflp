package jflp.instr;

import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

import org.jacoco.core.internal.analysis.filter.Filters;
import org.jacoco.core.internal.analysis.filter.IFilter;
import org.jacoco.core.internal.analysis.filter.IFilterContext;
import org.jacoco.core.internal.analysis.filter.IFilterOutput;
import org.objectweb.asm.Attribute;
import org.objectweb.asm.tree.AbstractInsnNode;
import org.objectweb.asm.tree.AnnotationNode;
import org.objectweb.asm.tree.ClassNode;
import org.objectweb.asm.tree.MethodNode;

/**
 * Instructions that do not stand for source code: compiler-generated methods,
 * duplicated finally blocks, try-with-resources and string-switch scaffolding,
 * and so on. Decided by JaCoCo 0.8.7's own filters, so a line is a code line
 * here exactly when it is one in JaCoCo, and so in Jaguar2.
 *
 * As in JaCoCo, an instruction does not count on its own line when a filter
 * ignores it or merges it into a representative copy (a finally block is
 * reported once, at the copy in its exception handler). A merged copy still
 * counts for its representative: running the finally block on the normal path
 * covers the finally lines.
 */
final class SourceFilter implements IFilterContext {

    private static final IFilter FILTERS = Filters.all();

    private final ClassNode c;
    private final Set<String> annotations = new HashSet<String>();
    private final Set<String> attributes = new HashSet<String>();

    SourceFilter(final ClassNode c) {
        this.c = c;
        addDescs(c.visibleAnnotations);
        addDescs(c.invisibleAnnotations);
        if (c.attrs != null) {
            for (final Object a : c.attrs) {
                attributes.add(((Attribute) a).type);
            }
        }
    }

    /** Filter verdict for one method. */
    static final class Result {
        final Set<AbstractInsnNode> ignored;
        /** Merged copy -> representative instruction. */
        final Map<AbstractInsnNode, AbstractInsnNode> merged;

        Result(final Set<AbstractInsnNode> ignored, final Map<AbstractInsnNode, AbstractInsnNode> merged) {
            this.ignored = ignored;
            this.merged = merged;
        }

        /** The instruction whose line this one counts for, or null when it counts for none. */
        AbstractInsnNode countsFor(final AbstractInsnNode i) {
            if (ignored.contains(i)) {
                return null;
            }
            final AbstractInsnNode r = representative(merged, i);
            return ignored.contains(r) ? null : r;
        }
    }

    Result filter(final MethodNode m) {
        final Set<AbstractInsnNode> ignored = new HashSet<AbstractInsnNode>();
        final Map<AbstractInsnNode, AbstractInsnNode> merged = new HashMap<AbstractInsnNode, AbstractInsnNode>();
        FILTERS.filter(m, this, new IFilterOutput() {
            public void ignore(final AbstractInsnNode from, final AbstractInsnNode to) {
                for (AbstractInsnNode i = from; i != to; i = i.getNext()) {
                    ignored.add(i);
                }
                ignored.add(to);
            }

            public void merge(final AbstractInsnNode i1, final AbstractInsnNode i2) {
                final AbstractInsnNode r1 = representative(merged, i1);
                final AbstractInsnNode r2 = representative(merged, i2);
                if (r1 != r2) {
                    merged.put(r2, r1);
                }
            }

            public void replaceBranches(final AbstractInsnNode source, final Set<AbstractInsnNode> newTargets) {
                // Affects branch counting only.
            }
        });
        return new Result(ignored, merged);
    }

    private static AbstractInsnNode representative(final Map<AbstractInsnNode, AbstractInsnNode> merged,
            AbstractInsnNode i) {
        AbstractInsnNode r;
        while ((r = merged.get(i)) != null) {
            i = r;
        }
        return i;
    }

    private void addDescs(final java.util.List<AnnotationNode> l) {
        if (l != null) {
            for (final AnnotationNode a : l) {
                annotations.add(a.desc);
            }
        }
    }

    public String getClassName() {
        return c.name;
    }

    public String getSuperClassName() {
        return c.superName;
    }

    public Set<String> getClassAnnotations() {
        return annotations;
    }

    public Set<String> getClassAttributes() {
        return attributes;
    }

    public String getSourceFileName() {
        return c.sourceFile;
    }

    public String getSourceDebugExtension() {
        return c.sourceDebug;
    }
}
